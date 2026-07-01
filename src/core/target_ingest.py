"""Target ingestion — the glue that completes the cross-device routing loop.

The pieces existed separately: per-firmware serial parsers (`src/protocols/*.parse_line` → ParsedEvent),
the shared `TargetPool` (→ `target.added`), and the `AutoRouter` (→ a command on another device). What
was missing was the wire between a connected device's serial output and the pool. `TargetIngestor.attach`
registers an `on_line` callback on a `SerialConnection` that runs the device's protocol parser and feeds
discovered APs/clients into the pool — so a scan on device A becomes a `target.added` event that the
AutoRouter can act on by commanding device B. That is the "one device gets an AP, another executes on it"
cross-resource path, end to end.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from src.models.target import Target, TargetType

log = logging.getLogger(__name__)


class TargetIngestor:
    """Bridges connected devices' serial output into a shared :class:`TargetPool`."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._attached: dict[str, Callable[[str], None]] = {}  # port -> on_line cb (for detach)

    def attach(self, conn: Any, protocol: Any) -> Callable[[str], None]:
        """Register an on_line handler on *conn* that parses each line with *protocol* and adds any
        discovered AP/client to the pool. *protocol* is any object with ``parse_line(line) -> ParsedEvent
        | None`` (e.g. ``src.protocols.marauder.MarauderProtocol``). Returns the callback (for detach)."""
        port = getattr(conn, "port", "?")
        # Idempotent re-attach: a co-owned connection (the persistent terminal still holds it) survives
        # a devices-tab disconnect, so open_connection returns the SAME object and a second attach would
        # stack a duplicate on_line -> every serial line parsed and pooled twice. Drop any prior first.
        prev = self._attached.get(port)
        remover = getattr(conn, "remove_line_callback", None)
        if prev is not None and callable(remover):
            try:
                remover(prev)
            except Exception:
                pass

        def on_line(line: str) -> None:
            try:
                ev = protocol.parse_line(line)
            except Exception:
                log.exception("TargetIngestor: parser error on %s", port)
                return
            if ev is None:
                return
            target = self._event_to_target(ev, port)
            if target is not None:
                self._pool.add(target)  # publishes 'target.added' -> AutoRouter

        conn.on_line(on_line)
        self._attached[port] = on_line
        log.info("TargetIngestor attached to %s via %s", port, type(protocol).__name__)
        return on_line

    def detach(self, conn: Any) -> None:
        """Best-effort removal of the on_line handler for *conn*."""
        port = getattr(conn, "port", "?")
        cb = self._attached.pop(port, None)
        remover = getattr(conn, "remove_line_callback", None)  # optional API
        if cb and callable(remover):
            try:
                remover(cb)
            except Exception:
                pass

    @staticmethod
    def _event_to_target(ev: Any, port: str) -> Target | None:
        """Map a ParsedEvent to a Target for the shared pool.

        Covers the identifier-bearing discovery events across all firmwares so a
        HaleHound / DIV / Marauder / BW16 device can feed the AutoRouter, not just
        WiFi APs: ap_found + Guardian rogue_ap -> AP; client_found -> CLIENT;
        ble_found -> BLE; subghz_found -> SUBGHZ (keyed by frequency, since SubGHz
        has no MAC); nfc_found -> NFC (keyed by UID). Events with no stable
        identifier (nrf24/mousejack/iot) stay terminal-only — they aren't pool
        targets. Unknown/info events return None.
        """
        d = getattr(ev, "data", {}) or {}
        et = getattr(ev, "event_type", "")

        if et in ("ap_found", "rogue_ap"):
            mac = str(d.get("bssid", "")).strip()
            idx = d.get("index")
            if not mac:
                # No BSSID (e.g. the BW16 Vampire scan prints index + SSID only). If the device gave a
                # per-scan index, keep the AP under a synthetic, SOURCE-TAGGED key so it can still be acted
                # on by THAT device's index-based actions (BW16 AT+DEAUTHIDX); otherwise it is unroutable
                # by MAC -> drop. (The synthetic key includes the port so two devices' indices never collide.)
                if idx is None:
                    return None
                mac = f"idx:{port}:{idx}"
            t = Target(
                mac=mac, target_type=TargetType.AP, ssid=str(d.get("ssid", "")),
                rssi=int(d.get("rssi", 0) or 0), channel=int(d.get("channel", 0) or 0),
                device_source=port,
            )
            if idx is not None:
                # Parser-supplied scan index (e.g. BW16's AT list ordinal). Enables this device's own
                # {index}-based TARGET_ACTIONS (source-restricted in the resolver). Firmwares that don't
                # emit an index leave this unset, so their index actions are dropped rather than guessed.
                t.extra["index"] = idx
            if et == "rogue_ap":
                t.extra["rogue"] = True  # HaleHound Guardian flagged this as a rogue/evil-twin
            return t

        if et == "client_found":
            mac = str(d.get("client_mac") or d.get("mac") or "").strip()
            if not mac:
                return None
            t = Target(
                mac=mac, target_type=TargetType.CLIENT, ssid="",
                rssi=int(d.get("rssi", 0) or 0), device_source=port,
            )
            idx = d.get("index")
            if idx is not None:
                # Parser-supplied station index -> enables the source-restricted {index} "Deauth Client"
                # action. Firmwares that don't emit an index leave it unset (the action is dropped, not
                # fired on a guessed index).
                t.extra["index"] = idx
            return t

        if et == "ble_found":
            mac = str(d.get("mac", "")).strip()
            if not mac:
                return None
            return Target(
                mac=mac, target_type=TargetType.BLE, ssid=str(d.get("name", "")),
                rssi=int(d.get("rssi", 0) or 0), device_source=port,
            )

        if et == "subghz_found":
            freq = str(d.get("frequency", "")).strip()
            if not freq:
                return None
            return Target(
                mac=freq, target_type=TargetType.SUBGHZ,
                # Firmwares disagree on field names: HaleHound emits 'modulation'/'data', the Flipper
                # emits 'protocol'/'key'. Fall back across both so a Flipper SubGHz capture keeps its
                # decoded protocol label AND its Key payload (the field that identifies the signal)
                # instead of landing in the pool blank.
                ssid=str(d.get("modulation") or d.get("protocol") or ""),
                rssi=int(d.get("rssi", 0) or 0), device_source=port,
                extra={"data": d.get("data") or d.get("key") or ""},
            )

        if et == "nfc_found":
            uid = str(d.get("uid", "")).strip()
            if not uid:
                return None
            return Target(
                mac=uid, target_type=TargetType.NFC,
                # Parsers emit 'nfc_type' (Flipper/HaleHound); keep 'type' as a tolerant fallback,
                # then degrade to the SAK byte — so the label is the tag type, not "08".
                ssid=str(d.get("nfc_type") or d.get("type") or d.get("sak", "")),
                device_source=port,
                extra={"atqa": d.get("atqa", ""), "sak": d.get("sak", "")},
            )

        if et == "rfid_found":
            # 125 kHz RFID (Flipper) — keyed by the tag serial (no MAC, like SubGHz keys on frequency),
            # routed to TargetType.RFID so the resolver picks 'rfid emulate' (not the NFC path).
            serial = str(d.get("uid") or d.get("data") or "").strip()
            if not serial:
                return None
            return Target(
                mac=serial, target_type=TargetType.RFID,
                ssid=str(d.get("rfid_type", "")), device_source=port,
                extra={"data": d.get("data", "")},
            )

        return None
