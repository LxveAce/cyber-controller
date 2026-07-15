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

from src.core import oui
from src.models.capture import CaptureRecord
from src.models.target import Target, TargetType

# Target types whose key is a real IEEE MAC, so an OUI→vendor lookup is meaningful. SubGHz (freq
# key), NFC/RFID (tag serials) and ALPR (already carries a vendor) are excluded — a serial that
# happened to be hex must never resolve to a phantom manufacturer.
_MAC_TARGET_TYPES = (TargetType.AP, TargetType.CLIENT, TargetType.BLE)

log = logging.getLogger(__name__)


class TargetIngestor:
    """Bridges connected devices' serial output into a shared :class:`TargetPool` (and, when given a
    :class:`~src.core.capture_store.CaptureStore`, the shared capture log too)."""

    def __init__(self, pool: Any, captures: Any = None, devices: Any = None) -> None:
        self._pool = pool
        self._captures = captures     # optional CaptureStore; None on the Devices-tab ingestor
        # Optional device registry (has get_device(port) -> Device|None, e.g. DeviceManager).
        # When given, a firmware that reports its own identity over serial (LxveOS status/info ->
        # device_info) updates its Device's live identity + runtime caps. None -> dropped, as
        # before (a device_info is neither a pool target nor a capture).
        self._devices = devices
        self._attached: dict[str, Callable[[str], None]] = {}  # port -> on_line cb (for detach)
        self._parsers: dict[str, Any] = {}  # port -> protocol instance (command sink resets it)
        self._recent_capture: dict[str, str] = {}  # port -> last capture key (for pcap_saved)

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
            # Invariant: ONE bad line/event must never break serial ingestion for the port — an
            # exception here would propagate into the serial reader and drop every LATER line.
            # parse_line is already guarded above; guard the downstream routing the same way, so a
            # raising _event_to_target (bad numeric coercion), a pool/capture add, or a device-info
            # update is logged and swallowed instead of killing the callback (_apply_device_info
            # keeps its own inner guards too).
            try:
                self._route(ev, port)
            except Exception:
                log.exception("TargetIngestor: routing error on %s", port)

        conn.on_line(on_line)
        self._attached[port] = on_line
        self._parsers[port] = protocol  # so send_to_port can reset scan ordinals on a list-clear
        log.info("TargetIngestor attached to %s via %s", port, type(protocol).__name__)
        return on_line

    def detach(self, conn: Any) -> None:
        """Best-effort removal of the on_line handler for *conn*."""
        port = getattr(conn, "port", "?")
        cb = self._attached.pop(port, None)
        self._parsers.pop(port, None)  # drop the per-port parser handle alongside its callback
        # Drop the port's pending pcap-attach target too, so a pcap written by the NEXT device to
        # occupy this port can't be attached to the previous device's stale handshake record.
        self._recent_capture.pop(port, None)
        remover = getattr(conn, "remove_line_callback", None)  # optional API
        if cb and callable(remover):
            try:
                remover(cb)
            except Exception:
                pass

    def parser_for(self, port: str) -> Any:
        """The protocol/parser instance the ingestor attached for *port* (or None). Lets the command
        sink reset a parser's scan ordinals when a device list-clear (`clearlist -a`/reboot) fires —
        the ordinals live on the per-connection parser, invisible to the pool."""
        return self._parsers.get(port)

    def note_command_sent(self, port: str, command: str) -> None:
        """Tell the ingestor a *command* was just written to *port* by ANY send path, so the port's
        parser can flush its scan ordinals when the command clears the device's list (`clearlist -a`
        / `-s`) or reboots it. Then the NEXT scan restarts `select ... {index}` at 0 and a Deauth-AP
        index binds to the right row. This lives on the ingestor (which owns the per-port parser) so
        EVERY door into the device shares it: the routed command sink (`CrossCommHub.send_to_port`)
        AND the Devices-tab terminal Send — a hand-typed clear would otherwise leave the ordinals
        stale (the same `select -a {index}` mis-bind, via a second write path). Never reset on a UI
        pool wipe: that sends no device command, so the on-device list is untouched and a reset
        would desync it. Parsers lacking a given reset method are skipped."""
        parser = self._parsers.get(port)
        if parser is None:
            return
        norm = " ".join(command.strip().lower().split())
        is_reboot = norm == "reboot" or norm.startswith("reboot ")
        if is_reboot or norm.startswith("clearlist -a"):
            fn = getattr(parser, "reset_scan_index", None)
            if callable(fn):
                fn()
            # Resetting the parser ordinal isn't enough: this port's pool AP targets seen before the
            # clear keep a stale extra['index'] mapping to a DIFFERENT row. Invalidate it so a
            # Deauth-AP {index} action is dropped until the AP is re-seen (which re-assigns a fresh
            # ordinal). Not-re-seen rows would otherwise mis-bind `select ap {index}` to a wrong AP.
            self._invalidate_pool_index(port, TargetType.AP)
        if is_reboot or norm.startswith("clearlist -s"):
            fn = getattr(parser, "reset_station_index", None)
            if callable(fn):
                fn()
            self._invalidate_pool_index(port, TargetType.CLIENT)  # station-list stale-index fix

    def _invalidate_pool_index(self, port: str, target_type: Any) -> None:
        """Clear the stale per-device scan index on this port's pool targets after its list was
        cleared (see note_command_sent). Guarded so a pool without invalidate_index (a stub) is a
        no-op, and any pool error is logged, never propagated into the send path."""
        fn = getattr(self._pool, "invalidate_index", None)
        if not callable(fn):
            return
        try:
            fn(port, target_type)
        except Exception:
            log.exception("TargetIngestor: pool index invalidation failed on %s", port)

    def _route(self, ev: Any, port: str) -> None:
        """Route one parsed event to the pool / capture log / device identity. Extracted from the
        ``on_line`` callback so a single try/except there enforces the "a bad event never breaks
        ingestion" invariant across the whole downstream path, not just ``parse_line``. Same
        branches, same order as the inline routing it replaced."""
        target = self._event_to_target(ev, port)
        if target is not None:
            if (not target.vendor and target.target_type in _MAC_TARGET_TYPES
                    and not target.mac.startswith("idx:")):
                # OUI→vendor enrichment; "" for unknown / randomized MACs (never fabricated). Skip
                # the MAC-less synthetic idx:{port}:{index} key: normalize_oui's 12-hex floor still
                # passes for a large index (idx:COM4:1000000000 -> "DC4100"), so a lookup could
                # fabricate a vendor for a MAC-less target — the no-phantom-vendor invariant.
                target.vendor = oui.lookup_vendor(target.mac)
            self._pool.add(target)  # publishes 'target.added' -> AutoRouter
        # Capture log — runs in addition to the target branch (a pcap_saved line has no Target
        # but still registers a capture). Only when a CaptureStore is given (the hub ingestor).
        if self._captures is not None:
            cap = self._event_to_capture(ev, port)
            if cap is not None:
                self._captures.add(cap)  # publishes 'capture.added' -> Crack Lab Captures list
        # Device identity — a firmware that reports itself over serial (LxveOS status/info ->
        # device_info) refreshes its Device's live identity + runtime capabilities. Additive to
        # the branches above (device_info is neither a target nor a capture). Only when a device
        # registry was given; otherwise dropped, as before.
        ev_type = getattr(ev, "event_type", "")
        if self._devices is not None:
            if ev_type == "device_info":
                self._apply_device_info(ev, port)
            elif ev_type == "arm_state":
                self._apply_arm_state(ev, port)
            elif ev_type == "alert":
                self._apply_alert(ev, port)

    def _apply_device_info(self, ev: Any, port: str) -> None:
        """Route a device_info event to the connected Device's live identity + runtime capabilities
        (best-effort). The registry lookup and the Device update are each guarded so a bad line or
        an exotic registry can never break serial ingestion."""
        try:
            dev = self._devices.get_device(port)
        except Exception:
            log.exception("TargetIngestor: device lookup failed on %s", port)
            return
        apply = getattr(dev, "apply_device_info", None)
        if callable(apply):
            try:
                apply(getattr(ev, "data", {}) or {})
            except Exception:
                log.exception("TargetIngestor: apply_device_info failed on %s", port)

    def _apply_arm_state(self, ev: Any, port: str) -> None:
        """Route an ``arm_state`` event (LxveOS ``arm``/``disarm``) to the connected Device's arm
        state, so the device tab can show an ARM/SAFE lamp. Same guarded shape as
        :meth:`_apply_device_info` — a bad line or exotic registry never breaks serial ingestion."""
        try:
            dev = self._devices.get_device(port)
        except Exception:
            log.exception("TargetIngestor: device lookup failed on %s", port)
            return
        apply = getattr(dev, "apply_arm_state", None)
        if callable(apply):
            try:
                apply(getattr(ev, "data", {}) or {})
            except Exception:
                log.exception("TargetIngestor: apply_arm_state failed on %s", port)

    def _apply_alert(self, ev: Any, port: str) -> None:
        """Route an ``alert`` event (a LxveOS detector firing) to the connected Device's alert state,
        so the device tab can surface it. Same guarded shape as :meth:`_apply_device_info` — a bad line
        or exotic registry never breaks serial ingestion."""
        try:
            dev = self._devices.get_device(port)
        except Exception:
            log.exception("TargetIngestor: device lookup failed on %s", port)
            return
        apply = getattr(dev, "apply_alert", None)
        if callable(apply):
            try:
                apply(getattr(ev, "data", {}) or {})
            except Exception:
                log.exception("TargetIngestor: apply_alert failed on %s", port)

    @staticmethod
    def _event_to_target(ev: Any, port: str) -> Target | None:
        """Map a ParsedEvent to a Target for the shared pool.

        Covers the identifier-bearing discovery events across all firmwares so a
        HaleHound / DIV / Marauder / BW16 device can feed the AutoRouter, not just
        WiFi APs: ap_found + Guardian rogue_ap -> AP; client_found -> CLIENT;
        ble_found -> BLE; subghz_found -> SUBGHZ (keyed by frequency+protocol+
        signal, since SubGHz has no MAC and one band carries many distinct
        signals); nfc_found -> NFC (keyed by UID). Events with no stable
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
            # Firmwares disagree on field names: HaleHound emits 'modulation'/'data', the Flipper
            # emits 'protocol'/'key'. Fall back across both so a Flipper SubGHz capture keeps its
            # decoded protocol label AND its Key payload (the field that identifies the signal)
            # instead of landing in the pool blank.
            proto = str(d.get("modulation") or d.get("protocol") or "")
            sig = str(d.get("data") or d.get("key") or "")
            # Key by frequency + protocol + signal payload, NOT frequency alone. A single band
            # (433.92 MHz is shared by countless remotes) carries many distinct signals; keying on
            # freq alone collapsed every one into a single target, so a second remote's capture
            # merely "updated" the first and vanished. Composing the identity keeps distinct signals
            # distinct while a genuine re-observation of the SAME signal still dedupes. Empty parts
            # drop out, so a freq-only device degrades to the old freq key (backward compatible), and
            # the raw frequency stays available in extra['frequency'] for any freq-only consumer.
            mac = ":".join(p for p in (freq, proto, sig) if p)
            return Target(
                mac=mac, target_type=TargetType.SUBGHZ,
                ssid=proto,
                rssi=int(d.get("rssi", 0) or 0), device_source=port,
                extra={"data": sig, "frequency": freq},
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

        if et == "alpr_found":
            # Flock-style ALPR surveillance camera, detected by Flock-You (WiFi OUI + probe-request IE
            # fingerprint). Keyed by the camera's MAC. Awareness-first: NO protocol declares TARGET_ACTIONS
            # for TargetType.ALPR, so the right-click / graph-node action MENUS offer zero actions on it — it
            # is a node you *see*, not a preset attack surface. (A user's own explicit AutoRouter wildcard
            # rule can still match any target type; consistent with the project's "label, never block" stance
            # it is unrouted by default rather than hard-blocked.)
            mac = str(d.get("mac") or d.get("mac_address") or "").strip()
            if not mac:
                return None
            t = Target(
                mac=mac, target_type=TargetType.ALPR,
                # Prefer the camera's SSID; fall back to the detection method so the row is never blank.
                ssid=str(d.get("ssid") or d.get("detection_method") or ""),
                rssi=int(d.get("rssi", 0) or 0), channel=int(d.get("channel", 0) or 0),
                device_source=port,
                vendor="Flock Safety (OUI/IE match)",
            )
            for extra_key in ("oui", "detection_method", "frequency"):
                if d.get(extra_key):
                    t.extra[extra_key] = d[extra_key]
            return t

        return None

    def _event_to_capture(self, ev: Any, port: str) -> CaptureRecord | None:
        """Map a ParsedEvent to a :class:`CaptureRecord` for the shared capture log — the WPA/WPA2
        handshake & PMKID capture events the firmwares emit but :meth:`_event_to_target` drops (not
        routable targets). Joins ssid/channel/rssi from the pool by BSSID so a captured handshake
        carries the network name it was advertising. Returns None for non-capture events.

        Only real crackable-material captures are logged: ``handshake_captured`` (EAPOL 4-way),
        ``pmkid_captured`` (an inline, directly-crackable PMKID) and ``pcap_saved`` (a written file,
        attached to the most-recent capture). GhostESP's ``capture`` event is an evil-portal
        CREDENTIAL grab (username/password), NOT a handshake, so it is deliberately excluded.
        """
        d = getattr(ev, "data", {}) or {}
        et = getattr(ev, "event_type", "")
        raw = getattr(ev, "raw", "") or ""

        if et == "handshake_captured":
            rec = CaptureRecord(bssid=str(d.get("bssid", "")).strip(), capture_type="eapol",
                                device_source=port, raw=raw)
            self._join_from_pool(rec)
            self._recent_capture[port] = rec.key
            return rec

        if et == "pmkid_captured":
            rec = CaptureRecord(bssid=str(d.get("bssid", "")).strip(), capture_type="pmkid",
                                pmkid=str(d.get("pmkid", "")).strip(), device_source=port, raw=raw)
            self._join_from_pool(rec)
            self._recent_capture[port] = rec.key
            return rec

        if et == "pcap_saved":
            path = str(d.get("path", "")).strip()
            recent_key = self._recent_capture.get(port)
            if (recent_key and self._captures is not None
                    and self._captures.get(recent_key) is not None):
                # Attach the file to the capture it belongs to (the handshake that just preceded it
                # on this port). One-shot: pop the key so a LATER unrelated pcap can't clobber this
                # record's pcap_path, and use attach_file so the write doesn't bump times_seen.
                self._captures.attach_file(recent_key, pcap_path=path)
                self._recent_capture.pop(port, None)
                return None
            # No preceding capture on the port: a bare pcap with no announced BSSID. Log it so
            # the file is tracked/crackable (it collapses under the empty key — accepted edge case).
            return CaptureRecord(bssid="", capture_type="eapol", pcap_path=path,
                                 device_source=port, raw=raw)

        return None

    def _join_from_pool(self, rec: CaptureRecord) -> None:
        """Fill ssid/channel/rssi on a capture from the matching pool AP (case-insensitive BSSID
        match), so a captured handshake carries the network it was seen advertising. Best-effort: a
        capture whose AP was never scanned just keeps its empty fields."""
        if not rec.bssid or self._pool is None:
            return
        try:
            aps = self._pool.by_type(TargetType.AP)
        except Exception:  # noqa: BLE001 — a non-standard pool must never break capture logging
            return
        want = rec.bssid.lower()
        for ap in aps:
            if str(getattr(ap, "mac", "")).lower() == want:
                if getattr(ap, "ssid", ""):
                    rec.ssid = ap.ssid
                if getattr(ap, "channel", 0):
                    rec.channel = ap.channel
                if getattr(ap, "rssi", 0):
                    rec.rssi = ap.rssi
                break
