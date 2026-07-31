"""Drive-trail (wardriving-v2 S3). The pure helpers -- trail_accept (append + fix check) and
trail_decimate (bounded down-sampling) -- are Qt-free and tested headless, like world_px. The
_TrailLayer + set_my_location wiring is exercised via a real (offscreen) FlockHeatmapTab below."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

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


# ── the _TrailLayer + set_my_location wiring, through a real offscreen FlockHeatmapTab ──

@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PyQt5.QtWidgets")           # widget tests skip without Qt; pure tests run
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def tab(qapp):
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab
    w = FlockHeatmapTab()
    yield w
    w.shutdown()
    w.deleteLater()
    qapp.processEvents()


def _drive(w, n, step=0.001):
    for i in range(n):
        w.set_my_location(37.77 + i * step, -122.42)   # each step is well past the threshold


def test_trail_layer_grows_with_gps_fixes(tab):
    _drive(tab, 5)
    assert len(tab._trail) == 5                          # every moved fix recorded
    assert tab._trail_layer is not None
    assert len(tab._trail_layer._points) == 5            # projected into a world_px polyline


def test_trail_ignores_sub_threshold_moves(tab):
    d = _TRAIL_MIN_MOVE_DEG
    tab.set_my_location(37.77, -122.42)                  # first breadcrumb
    tab.set_my_location(37.77 + d / 3, -122.42)          # tiny move -> skipped
    tab.set_my_location(37.77 + d * 2 / 3, -122.42)      # still within threshold of last -> skipped
    assert len(tab._trail) == 1                          # standing still doesn't grow the trail


def test_trail_toggle_hides_and_restores(tab):
    _drive(tab, 4)
    assert tab._trail_layer is not None
    tab._chk_trail.setChecked(False)                     # hide the trail
    assert tab._trail_layer is None                      # layer dropped...
    assert len(tab._trail) == 4                          # ...but the drive data is retained
    tab._chk_trail.setChecked(True)
    assert tab._trail_layer is not None                  # ...and restored


def test_trail_survives_rebuild_and_renders(tab):
    _drive(tab, 4)
    tab._rebuild()                                       # a basemap/AP toggle clears + redraws
    assert tab._trail_layer is not None
    assert len(tab._trail_layer._points) == 4            # re-added from the retained trail
    tab.render_native()                                  # a trail + pin must not crash the render


# ── trail persist/replay serialization (owner-call #2) — pure, headless ──

def test_trail_geojson_roundtrip():
    from src.ui.qt.flock_heatmap_tab import trail_from_geojson, trail_to_geojson
    trail = [(37.77, -122.42), (37.78, -122.41), (37.79, -122.40)]
    gj = trail_to_geojson(trail)
    assert gj["features"][0]["geometry"]["type"] == "LineString"
    assert gj["features"][0]["geometry"]["coordinates"][0] == [-122.42, 37.77]   # [lon, lat] order
    assert trail_from_geojson(gj) == trail                        # round-trips exactly


def test_trail_from_geojson_is_tolerant():
    from src.ui.qt.flock_heatmap_tab import trail_from_geojson
    assert trail_from_geojson({}) == []                          # empty / no features
    assert trail_from_geojson("not a dict") == []               # junk
    assert trail_from_geojson({"features": [{"geometry": {"type": "Point",
                                             "coordinates": [1, 2]}}]}) == []   # not a LineString
    gj = {"features": [{"geometry": {"type": "LineString", "coordinates": [
        [-122.4, 37.7], ["x", 1], [200.0, 10.0], [float("nan"), 5.0], [-122.5, 37.8]]}}]}
    assert trail_from_geojson(gj) == [(37.7, -122.4), (37.8, -122.5)]   # bad coords dropped
