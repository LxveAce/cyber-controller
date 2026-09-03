"""Live GPS position tracker for the web UI.

Fed one raw serial line at a time from every connected device (attached as a line callback at
connect time). It keeps only the most recent valid NMEA fix plus a monotonic timestamp, so a reader
like the Flock GPS-follow map can ask "where am I now, and how stale is that?" without touching serial.

Thread-safe: the update() calls run on device reader threads; snapshot() runs on Flask request threads.
Cheap by design — non-NMEA lines (the vast majority of a firmware's chatter) are rejected before any
parse, so attaching this to a busy serial stream costs almost nothing.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from src.core.wardrive import GpsFix, parse_nmea


class GpsTracker:
    """Holds the latest GPS fix seen on any connected device's serial stream."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fix: Optional[GpsFix] = None
        self._at: float = 0.0  # time.monotonic() when the current fix was stored

    def update(self, line: str) -> None:
        """Feed one serial line. Stores it only if it parses to a valid GPS fix; no-op otherwise.

        Fast-rejects anything that isn't a GGA/RMC sentence before the parser runs, so this stays
        cheap on a firmware pumping scan/attack chatter that has nothing to do with GPS.
        """
        if not line or ("GGA" not in line and "RMC" not in line):
            return
        try:
            fix = parse_nmea(line)
        except Exception:
            return
        if fix is not None and fix.has_fix:
            with self._lock:
                self._fix = fix
                self._at = time.monotonic()

    def snapshot(self, max_age_s: float = 30.0) -> dict:
        """The current fix as a plain dict for JSON, or an honest no-fix when absent or stale.

        A fix older than *max_age_s* is reported as no-fix (with ``stale``/``age_s``) so the map
        stops following a position the device is no longer confirming.
        """
        with self._lock:
            fix, at = self._fix, self._at
        if fix is None:
            return {"has_fix": False}
        age = time.monotonic() - at
        if age > max_age_s:
            return {"has_fix": False, "stale": True, "age_s": round(age, 1)}
        return {
            "has_fix": True,
            "lat": round(fix.lat, 6),
            "lon": round(fix.lon, 6),
            "utc": fix.utc,
            "sats": fix.sats,
            "age_s": round(age, 1),
        }

    def reset(self) -> None:
        """Drop the held fix (used by tests and on a full disconnect)."""
        with self._lock:
            self._fix = None
            self._at = 0.0


# Process-wide default instance — the web app attaches its update() to every connection and reads it
# from /api/gps. One tracker is correct: it holds "the operator's current position", not per-device state.
_default = GpsTracker()


def get_tracker() -> GpsTracker:
    return _default
