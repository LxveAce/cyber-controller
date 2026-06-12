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
            if not mac:  # e.g. the BW16 Vampire scan prints no BSSID — not routable by MAC
                return None
            t = Target(
                mac=mac, target_type=TargetType.AP, ssid=str(d.get("ssid", "")),
                rssi=int(d.get("rssi", 0) or 0), channel=int(d.get("channel", 0) or 0),
                device_source=port,
            )
            if et == "rogue_ap":
                t.extra["rogue"] = True  # HaleHound Guardian flagged this as a rogue/evil-twin
            return t

        if et == "client_found":
            mac = str(d.get("client_mac") or d.get("mac") or "").strip()
            if not mac:
                return None
            return Target(
                mac=mac, target_type=TargetType.CLIENT, ssid="",
                rssi=int(d.get("rssi", 0) or 0), device_source=port,
            )

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
                ssid=str(d.get("modulation", "")),
                rssi=int(d.get("rssi", 0) or 0), device_source=port,
                extra={"data": d.get("data", "")},
            )

        if et == "nfc_found":
            uid = str(d.get("uid", "")).strip()
            if not uid:
                return None
            return Target(
                mac=uid, target_type=TargetType.NFC,
                ssid=str(d.get("type", d.get("sak", ""))),
                device_source=port,
                extra={"atqa": d.get("atqa", ""), "sak": d.get("sak", "")},
            )

        return None
