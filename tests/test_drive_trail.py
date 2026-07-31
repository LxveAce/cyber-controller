"""Pure drive-trail helpers (wardriving-v2 S3): trail_accept (append threshold + fix validation) and
trail_decimate (bounded down-sampling). Qt-free -- they live in the pure core, so the
append/threshold/decimate logic is unit-testable headless, like world_px."""
from __future__ import annotations

from src.ui.qt.flock_heatmap_tab import _TRAIL_MIN_MOVE_DEG, trail_accept, trail_decimate


def test_trail_accept_first_point_and_movement():
    d = _TRAIL_MIN_MOVE_DEG
    assert trail_accept(None, 37.77, -122.42) is True            # first fix starts the trail
    last = (37.77, -122.42)
    assert trail_accept(last, 37.77, -122.42) is False           # standing still -> no breadcrumb
    assert trail_accept(last, 37.77 + d / 2, -122.42) is False   # within threshold -> skip
    assert trail_accept(last, 37.77 + d * 2, -122.42) is True    # moved far enough -> record
    assert trail_accept(last, 37.77, -122.42 - d * 2) is True    # lon movement counts (Chebyshev)


def test_trail_accept_rejects_bad_fixes():
    last = (37.77, -122.42)
    assert trail_accept(last, float("nan"), -122.42) is False    # non-finite
    assert trail_accept(last, 37.77, float("inf")) is False
    assert trail_accept(None, 120.0, 10.0) is False              # lat out of range
    assert trail_accept(None, 10.0, 200.0) is False              # lon out of range
    assert trail_accept(None, 0.0, 0.0) is False                 # Null-Island no-fix sentinel


def test_trail_decimate_bounds_a_long_drive():
    assert trail_decimate([(0.0, 0.0)], 5000) == [(0.0, 0.0)]    # under cap -> unchanged
    trail = [(float(i), 0.0) for i in range(10001)]
    small = trail_decimate(trail, 5000)
    assert len(small) <= 5000                                    # bounded
    assert small[0] == trail[0]                                  # start preserved
    assert small == trail[::((10001 + 4999) // 5000)]            # evenly strided (keeps the shape)


def test_trail_decimate_edge_caps():
    trail = [(float(i), 0.0) for i in range(100)]
    assert trail_decimate(trail, 0) == [trail[0]]               # max_points < 1 treated as 1
    assert len(trail_decimate(trail, 1)) == 1
    assert trail_decimate(trail, 100) is trail                  # exactly at cap -> same object
    assert trail_decimate(trail, 1000) is trail                 # over capacity -> unchanged
