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
        """Map a ParsedEvent to a Target (only the location-bearing discovery events)."""
        d = getattr(ev, "data", {}) or {}
        et = getattr(ev, "event_type", "")
        if et == "ap_found":
            mac = str(d.get("bssid", "")).strip()
            if not mac:
                return None
            return Target(
                mac=mac, target_type=TargetType.AP, ssid=str(d.get("ssid", "")),
                rssi=int(d.get("rssi", 0) or 0), channel=int(d.get("channel", 0) or 0),
                device_source=port,
            )
        if et == "client_found":
            mac = str(d.get("client_mac", "")).strip()
            if not mac:
                return None
            return Target(
                mac=mac, target_type=TargetType.CLIENT, ssid="",
                device_source=port,
            )
        return None
