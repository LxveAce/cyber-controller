"""Multi-board wardrive controller (F1 slice 4a).

Runs a GPS-tagged wardrive across SEVERAL boards at once, all feeding one :class:`MultiWardriveSession`:
a single shared GPS fix gates every board, each board's AP stream is routed through the DeviceManager
(owner-tagged, so a board already open elsewhere is shared, not double-opened), and every sighting merges
into one standards-compliant WiGLE CSV with per-board first-seen attribution.

This is the framework-agnostic core the Qt panel wraps — no Qt here, so it's unit-testable against a fake
DeviceManager. An optional ``on_update`` callback fires after each line so a UI can refresh its rows.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional, TextIO

from src.core.wardrive import MultiWardriveSession, scan_commands_for


class MultiWardriveController:
    """Own N per-board captures feeding one shared session + GPS. Call :meth:`add_board` before
    :meth:`start`, then :meth:`stop` to tear everything down."""

    OWNER = "wardrive-multi"

    def __init__(self, device_manager: Any, out: TextIO, gps_port: str = "", gps_baud: int = 9600,
                 on_update: Optional[Callable[[], None]] = None) -> None:
        self._dm = device_manager
        self._out = out
        self._gps_port, self._gps_baud = gps_port, gps_baud
        self._on_update = on_update or (lambda: None)
        self._lock = threading.Lock()
        self._session = MultiWardriveSession(out)
        self._boards: list[dict] = []          # {port, baud, firmware, stop_cmd, conn, cb}
        self._gps_conn = None
        self._gps_cb = None
        self._running = False
        self.errors: list[tuple[str, str]] = []  # (port, message) for boards that failed to start

    # ── configuration (before start) ────────────────────────────────
    def add_board(self, port: str, baud: int = 115200, firmware: str = "") -> None:
        if self._running:
            raise RuntimeError("cannot add a board while the run is active")
        self._boards.append({"port": port, "baud": baud, "firmware": firmware,
                             "stop_cmd": "stopscan", "conn": None, "cb": None})
        self._session.add_board(port)

    @property
    def board_ports(self) -> list[str]:
        return [b["port"] for b in self._boards]

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self) -> None:
        if self._running or not self._boards:
            return
        self._session.start()
        self._running = True
        if self._gps_port:
            try:
                self._gps_conn = self._dm.open_connection(self._gps_port, self._gps_baud, owner=self.OWNER)
                self._gps_cb = self._on_gps_line
                self._gps_conn.on_line(self._gps_cb)
            except Exception as exc:  # noqa: BLE001 — a missing GPS just means no rows, not a crash
                self.errors.append((self._gps_port, f"gps: {exc}"))
                self._gps_conn = None
        for b in self._boards:
            conn = cb = None
            try:
                conn = self._dm.open_connection(b["port"], b["baud"], owner=self.OWNER)
                cb = self._make_dev_cb(b["port"])
                conn.on_line(cb)
                cmds = scan_commands_for(b["firmware"])
                b["stop_cmd"] = cmds.stop
                try:
                    conn.line_ending = cmds.line_ending
                except Exception:  # noqa: BLE001
                    pass
                for start_cmd in cmds.start:
                    conn.write(start_cmd)
                b["conn"], b["cb"] = conn, cb
            except Exception as exc:  # noqa: BLE001 — isolate one bad board from the rest of the deck
                self.errors.append((b["port"], str(exc)))
                if conn is not None:               # opened but failed mid-start: don't leak the port/callback
                    for step in (lambda c=conn, f=cb: c.remove_line_callback(f) if f is not None else None,
                                 lambda p=b["port"]: self._dm.close_connection(p, owner=self.OWNER)):
                        try:
                            step()
                        except Exception:  # noqa: BLE001
                            pass
        self._on_update()

    def stop(self) -> None:
        if not self._running and self._gps_conn is None and not any(b["conn"] for b in self._boards):
            return
        self._running = False
        for b in self._boards:                 # tear boards down first (drains their reader threads)
            conn, cb = b["conn"], b["cb"]
            if conn is None:
                continue
            for step in (lambda c=conn, s=b["stop_cmd"]: c.write(s),
                         lambda c=conn, f=cb: c.remove_line_callback(f),
                         lambda p=b["port"]: self._dm.close_connection(p, owner=self.OWNER)):
                try:
                    step()
                except Exception:  # noqa: BLE001
                    pass
            b["conn"], b["cb"] = None, None
        if self._gps_conn is not None:
            for step in (lambda: self._gps_conn.remove_line_callback(self._gps_cb),
                         lambda: self._dm.close_connection(self._gps_port, owner=self.OWNER)):
                try:
                    step()
                except Exception:  # noqa: BLE001
                    pass
            self._gps_conn = None
        try:
            self._out.flush()
        except Exception:  # noqa: BLE001
            pass
        self._on_update()

    # ── line callbacks (fire on the DeviceManager reader threads) ────
    def _make_dev_cb(self, port: str) -> Callable[[str], None]:
        def _cb(line: str) -> None:
            if not line:
                return
            with self._lock:
                if not self._running:
                    return
                self._session.observe(port, line)
            self._on_update()
        return _cb

    def _on_gps_line(self, line: str) -> None:
        if not line:
            return
        with self._lock:
            if not self._running:
                return
            self._session.update_gps(line)
        self._on_update()

    # ── aggregate status (for the UI) ────────────────────────────────
    @property
    def ap_count(self) -> int:
        return self._session.ap_count

    @property
    def has_fix(self) -> bool:
        return self._session.has_fix

    @property
    def fix_text(self) -> str:
        fix = self._session.fix
        return f"{fix.lat:.5f}, {fix.lon:.5f}" if (fix and fix.has_fix) else "No Fix"

    def snapshot(self) -> dict:
        """A thread-safe view for the UI: aggregate + per-board counts."""
        with self._lock:
            per_board = dict(self._session.per_board)
            total = self._session.ap_count
            fix_text = self.fix_text
        return {
            "running": self._running,
            "fix": fix_text,
            "total_aps": total,
            "boards": [
                {"port": b["port"], "firmware": b["firmware"],
                 "aps": per_board.get(b["port"], 0), "started": b["conn"] is not None}
                for b in self._boards
            ],
            # per-board / GPS open failures from start(), surfaced so a board that silently failed
            # to open is visible (otherwise it just never appears as started, with no reason shown).
            "errors": list(self.errors),
        }
