"""Capture correlation (punch-list #2, slice 5): tie a fired deauth to the handshake it produces.

A pure-core, bus-only observer — the "capture-confirm window" from the design. When a deauth/capture
action fires (an ``action.executed`` carrying the target BSSID and a non-empty ``chain_events`` —
the hook declared but until now unused on :class:`~src.models.action.TargetAction`) it arms a
bounded window for that ``(bssid, port)``. If a ``capture.added`` / ``capture.updated`` for the same
BSSID lands inside the window, it publishes ``capture.confirmed`` — the "the deauth worked, here's
the handshake" signal the Qt layer turns into an activity-log line (the Captures row already shows).

It consumes events the parsers already emit and issues NO commands and authors NO 802.11 frames —
this is correlation, not new radio behaviour. Windows that pass with no capture are pruned lazily
(so a much-later capture can't fire a false "confirmed"); an explicit :meth:`sweep` (driven by a Qt
timer) publishes ``capture.timeout`` for the honest "no handshake within the window" case.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

# A send that failed or was declined must not arm a window. The Targets-tab action.executed payload
# carries a status; these are the "the command actually went out" values (others -> don't arm).
_SENT_STATUSES = ("success", "sent", "ok")


class CaptureCorrelator:
    """Ties a fired deauth/capture action to the handshake it produces, over the shared bus."""

    DEFAULT_WINDOW_S = 20.0

    def __init__(self, bus, clock: Callable[[], float] | None = None,
                 window_s: float = DEFAULT_WINDOW_S) -> None:
        self._bus = bus
        self._clock = clock or time.monotonic
        self._window_s = window_s
        # (bssid_lower, port) -> {"deadline", "action", "armed_at", "bssid"}
        self._pending: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()
        bus.subscribe("action.executed", self._on_action)
        bus.subscribe("capture.added", self._on_capture)
        bus.subscribe("capture.updated", self._on_capture)

    # ── arming ───────────────────────────────────────────────────────
    def _on_action(self, _topic: str, payload: dict) -> None:
        """Arm a window when a chain-event-bearing action fires against a known BSSID."""
        if not payload.get("chain_events"):
            return
        bssid = str(payload.get("target_mac") or payload.get("bssid") or "").strip()
        if not bssid:
            return
        status = payload.get("status")
        if status is not None and status is not True and status not in _SENT_STATUSES:
            return                                    # failed/declined send -> no window
        port = str(payload.get("port") or payload.get("device") or "")
        self.arm(bssid, port, str(payload.get("action") or ""))

    def arm(self, bssid: str, port: str, action_name: str = "",
            window_s: float | None = None) -> None:
        """Open a capture-confirm window for *bssid* on *port* (usually via ``action.executed``)."""
        now = self._clock()
        deadline = now + (self._window_s if window_s is None else window_s)
        with self._lock:
            self._prune_locked(now)
            self._pending[(bssid.lower(), port)] = {
                "deadline": deadline, "action": action_name, "armed_at": now, "bssid": bssid}

    # ── confirming ───────────────────────────────────────────────────
    def _on_capture(self, _topic: str, payload: dict) -> None:
        """A capture landed — if it matches an armed window, publish ``capture.confirmed``."""
        bssid = str(payload.get("bssid") or "").strip()
        if not bssid:
            return
        port = str(payload.get("device_source") or "")
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            meta = self._pending.pop((bssid.lower(), port), None)
            if meta is None:
                # The capture's port may be blank or differ from the arm port; match on BSSID alone.
                for k in list(self._pending):
                    if k[0] == bssid.lower():
                        meta = self._pending.pop(k)
                        break
        if meta is None:
            return
        elapsed = max(0.0, now - meta["armed_at"])
        self._bus.publish("capture.confirmed", {
            "bssid": payload.get("bssid"),
            "device": port or None,
            "action": meta["action"],
            "capture_type": payload.get("capture_type", ""),
            "elapsed_s": round(elapsed, 1),
        })

    # ── expiry ───────────────────────────────────────────────────────
    def _prune_locked(self, now: float) -> None:
        """Drop expired windows silently (caller holds the lock) so no stale confirm can fire."""
        for k in [k for k, m in self._pending.items() if m["deadline"] <= now]:
            del self._pending[k]

    def sweep(self) -> list[str]:
        """Publish ``capture.timeout`` for every window that has expired; return their BSSIDs.

        Driven by a Qt timer so the honest "no handshake within the window" case is surfaced with
        correct timing (rather than only when a later event happens to prune it)."""
        now = self._clock()
        with self._lock:
            timed_out = [self._pending.pop(k) for k, m in list(self._pending.items())
                         if m["deadline"] <= now]
        for meta in timed_out:
            self._bus.publish("capture.timeout", {
                "bssid": meta["bssid"], "action": meta["action"], "window_s": self._window_s})
        return [meta["bssid"] for meta in timed_out]

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)
