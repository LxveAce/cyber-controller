"""GpsTracker — the live-position holder behind the Flock GPS-follow map."""
from __future__ import annotations

import time

from src.core.gps_tracker import GpsTracker

# A valid GGA fix (37.86N, 122.2W, 8 sats) and an RMC fix, both real NMEA.
_GGA = "$GPGGA,123519,3751.65,N,12212.00,W,1,08,0.9,545.4,M,46.9,M,,*47"
_RMC = "$GPRMC,123519,A,3751.65,N,12212.00,W,022.4,084.4,230394,003.1,W*6A"


def test_empty_tracker_has_no_fix():
    assert GpsTracker().snapshot() == {"has_fix": False}


def test_gga_line_produces_a_fix():
    t = GpsTracker()
    t.update(_GGA)
    snap = t.snapshot()
    assert snap["has_fix"] is True
    assert round(snap["lat"], 2) == 37.86
    assert round(snap["lon"], 2) == -122.20
    assert snap["sats"] == 8


def test_rmc_line_produces_a_fix():
    t = GpsTracker()
    t.update(_RMC)
    assert t.snapshot()["has_fix"] is True


def test_non_nmea_lines_are_ignored_cheaply():
    t = GpsTracker()
    t.update(_GGA)
    t.update("18 APs, 41 stations")      # marauder chatter, no GGA/RMC
    t.update("[flash] Hash of data verified")
    t.update("")
    # the earlier fix is untouched, and none of the junk crashed or overwrote it
    assert t.snapshot()["has_fix"] is True


def test_stale_fix_reads_as_no_fix():
    t = GpsTracker()
    t.update(_GGA)
    t._at = time.monotonic() - 100  # pretend the fix is 100s old
    snap = t.snapshot(max_age_s=30.0)
    assert snap["has_fix"] is False
    assert snap.get("stale") is True


def test_reset_clears_the_fix():
    t = GpsTracker()
    t.update(_GGA)
    t.reset()
    assert t.snapshot() == {"has_fix": False}
