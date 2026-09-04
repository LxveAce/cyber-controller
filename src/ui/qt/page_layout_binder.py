"""Bind a PageLayout frame's live slots to the CrossCommHub.

Additive adapter: reads the hub's existing surface (pool / captures / dm / bus) and pushes it into
the frame's badge + status slots, and mirrors the shell's display posture into src.core.posture.
The frame stays app-agnostic and the hub is untouched; this is the only object that knows both.
Nothing here mutates the hub or main_window — it just observes and updates the shell.
"""
from __future__ import annotations

from src.core import posture as posture_state
from src.ui.qt.page_layout import PageLayout
from src.ui.qt.theme import colors as C


class PageLayoutBinder:
    """Push live hub/bus data into a PageLayout's slots + gate posture escalation to Offense."""

    def __init__(self, layout: PageLayout, hub) -> None:
        self._layout = layout
        self._hub = hub
        bus = getattr(hub, "bus", None)
        if bus is not None:
            for t in ("target.added", "target.updated", "target.removed", "target.cleared"):
                bus.subscribe(t, self._on_target_event)
            for t in ("capture.added", "capture.removed", "capture.cleared", "capture.cracked"):
                bus.subscribe(t, self._on_capture_event)
        # Mirror the shell's VISIBLE display posture into src.core.posture so any surface can read
        # the current indicator. Sync the initial state now, then track every host-driven change
        # (which routes through PageLayout.set_posture -> posture_changed). Display only — the real
        # send-path floor is safety.classify + the OPERATE two-factor arm, which this never touches.
        layout.posture_changed.connect(posture_state.set_posture)
        posture_state.set_posture(layout.posture)
        self.refresh()

    # ── live counts ──────────────────────────────────────────────────
    def _pool_count(self) -> int:
        pool = getattr(self._hub, "pool", None)
        return int(getattr(pool, "count", 0) or 0) if pool is not None else 0

    def _capture_count(self) -> int:
        caps = getattr(self._hub, "captures", None)
        return int(getattr(caps, "count", 0) or 0) if caps is not None else 0

    def refresh(self) -> None:
        """Push current counts + device status into the frame (call once on wire-up)."""
        self._layout.set_badge("targets", self._pool_count())
        self._layout.set_badge("captures", self._capture_count())
        self._push_device_status()

    def refresh_devices(self) -> None:
        """Re-push device-truth status (link count + ARMED). The target/capture badges are already
        live via bus events, but device status only ran at construction — a host must call this when
        the device set changes (connect/disconnect) so the shell's count + ARMED don't go stale."""
        self._push_device_status()

    def _on_target_event(self, _topic: str, _payload: dict) -> None:
        self._layout.set_badge("targets", self._pool_count())

    def _on_capture_event(self, _topic: str, _payload: dict) -> None:
        self._layout.set_badge("captures", self._capture_count())

    # ── device-truth status ──────────────────────────────────────────
    def _push_device_status(self) -> None:
        dm = getattr(self._hub, "dm", None)
        if dm is None or not hasattr(dm, "list_connected"):
            return
        devs = list(dm.list_connected())
        n = len(devs)
        self._layout.set_status("link", (f"{n} device" + ("" if n == 1 else "s")) if n else "",
                                color=C.SUCCESS if n else None)
        # ARMED is the safety-relevant truth: a connected device reporting offensive-TX armed.
        states = [getattr(d, "arm_state", "") for d in devs]
        if "armed" in states:
            self._layout.set_status("armed", "ARMED", color=C.ERROR)
        elif "pending" in states:
            self._layout.set_status("armed", "ARMING", color=C.WARNING)
        else:
            self._layout.set_status("armed", "")
