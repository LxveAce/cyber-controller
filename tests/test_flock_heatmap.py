"""Flock heatmap (FL F4) — web-mercator projection core (pure) + the offscreen heatmap widget.

The projection/heat helpers are unit-tested with no Qt; the widget is rendered offscreen into a QImage and
asserted on (camera count, scene items, non-background pixels). Fed by F2's FlockSession GeoJSON.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from src.ui.qt.flock_heatmap_tab import MercatorFit, heat_color, web_mercator

# ── pure projection core (no Qt) ─────────────────────────────────────


def test_web_mercator_reference_points():
    x, y = web_mercator(0.0, 0.0)
    assert abs(x - 0.5) < 1e-9 and abs(y - 0.5) < 1e-9       # equator/prime-meridian -> center
    assert abs(web_mercator(0.0, 180.0)[0] - 1.0) < 1e-9     # antimeridian east -> x=1
    assert abs(web_mercator(0.0, -180.0)[0] - 0.0) < 1e-9    # west -> x=0
    assert web_mercator(45.0, 0.0)[1] < 0.5                  # north -> smaller y (top / screen-up)
    assert web_mercator(-45.0, 0.0)[1] > 0.5                 # south -> larger y (bottom)


def test_web_mercator_clamps_poles():
    y_hi = web_mercator(89.9, 0.0)[1]
    assert abs(y_hi) < 0.01                                  # clamped to the top edge, finite (no log blow-up)


def test_mercator_fit_two_points_within_canvas():
    fit = MercatorFit([(0.0, 0.0), (0.0, 90.0)], 800, 600, pad=24)
    p_west = fit.to_pixel(0.0, 0.0)
    p_east = fit.to_pixel(0.0, 90.0)
    assert p_west[0] < p_east[0]                             # west is left of east
    for p in (p_west, p_east):
        assert 24 <= p[0] <= 776 and 24 <= p[1] <= 576       # inside the padded canvas
    assert abs(p_west[1] - p_east[1]) < 1e-6                 # same latitude -> same y


def test_mercator_fit_single_point_centers():
    fit = MercatorFit([(10.0, 10.0)], 800, 600)
    assert fit.to_pixel(10.0, 10.0) == (400.0, 300.0)        # degenerate -> centered, no div-by-zero


def test_mercator_fit_identical_points_no_crash():
    fit = MercatorFit([(5.0, 5.0), (5.0, 5.0), (5.0, 5.0)], 800, 600)
    assert fit.to_pixel(5.0, 5.0) == (400.0, 300.0)


def test_heat_color_ramp():
    assert heat_color(0.0) == (31, 119, 180)                 # cool blue
    assert heat_color(1.0) == (214, 39, 40)                  # hot red
    assert heat_color(0.0)[0] < heat_color(1.0)[0]           # red rises with density
    assert heat_color(-5) == heat_color(0.0)                 # clamped
    assert heat_color(9) == heat_color(1.0)


# ── the offscreen widget ─────────────────────────────────────────────

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core.flock import FlockSession  # noqa: E402
from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab  # noqa: E402

FIX_A = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
FIX_B = "$GPGGA,123520,4808.000,N,01132.000,E,1,08,0.9,545.4,M,46.9,M,,*00"
DET_1 = '{"event":"detection","mac_address":"AA:BB:CC:DD:EE:FF","rssi":-50,"channel":6}'
DET_2 = '{"event":"detection","mac_address":"11:22:33:44:55:66","rssi":-60,"channel":1}'


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _session_two_cameras():
    s = FlockSession()
    s.update_gps(FIX_A)
    s.observe(DET_1)
    s.update_gps(FIX_B)
    s.observe(DET_2)
    return s


def _has_non_bg_pixel(img) -> bool:
    from PyQt5.QtGui import QColor
    bg = QColor("#0d1117").rgb()
    for y in range(0, img.height(), 4):
        for x in range(0, img.width(), 4):
            if img.pixel(x, y) != bg:
                return True
    return False


def test_widget_empty_is_safe(qapp):
    w = FlockHeatmapTab()
    assert w.camera_count == 0
    img = w.render_native()                                 # must not crash on an empty scene
    assert (img.width(), img.height()) == (800, 600)


def test_widget_renders_cameras_from_session(qapp):
    w = FlockHeatmapTab()
    w.set_session(_session_two_cameras())
    assert w.camera_count == 2
    assert len(w._camera_items) == 2                        # one scene dot per camera
    assert _has_non_bg_pixel(w.render_native())             # something was actually drawn


def test_widget_set_geojson_filters_invalid(qapp):
    w = FlockHeatmapTab()
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [11.0, 48.0]}, "properties": {"count": 3}},
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}, "properties": {}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": ["x", None]}, "properties": {}},
        {"type": "Feature", "geometry": None, "properties": {}},
    ]}
    w.set_geojson(gj)
    assert w.camera_count == 1                              # only the valid Point survived


def test_widget_identical_coords_no_crash(qapp):
    w = FlockHeatmapTab()
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [11.0, 48.0]}, "properties": {"count": 1}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [11.0, 48.0]}, "properties": {"count": 9}},
    ]}
    w.set_geojson(gj)
    assert w.camera_count == 2
    w.render_native()                                       # degenerate bbox -> centered, no div-by-zero


def test_widget_load_geojson_file_roundtrip(qapp, tmp_path):
    p = tmp_path / "cameras.geojson"
    with open(p, "w", encoding="utf-8") as fh:
        _session_two_cameras().write_geojson(fh)
    w = FlockHeatmapTab()
    n = w.load_geojson_file(str(p))
    assert n == 2 and w.camera_count == 2


def test_widget_loads_a_session_checkpoint(qapp, tmp_path):
    # A live-drive checkpoint (FlockSession.checkpoint, atomic, written after each add) must load
    # straight into the map — locks the persist->map contract so the offline map can resume a drive.
    p = tmp_path / "drive" / "flock.geojson"  # checkpoint creates the parent dir
    assert _session_two_cameras().checkpoint(p) == 2
    w = FlockHeatmapTab()
    assert w.load_geojson_file(str(p)) == 2 and w.camera_count == 2


def test_widget_load_bad_file_is_safe(qapp, tmp_path):
    bad = tmp_path / "nope.geojson"
    bad.write_text("{ not json", encoding="utf-8")
    w = FlockHeatmapTab()
    assert w.load_geojson_file(str(bad)) == 0               # bad file -> 0, no crash
    assert w.load_geojson_file(str(tmp_path / "missing.geojson")) == 0


def test_mercator_fit_empty_is_safe():
    fit = MercatorFit([], 800, 600)                          # must not raise on min()/max()
    assert fit.to_pixel(0.0, 0.0) == (400.0, 300.0)


def test_mercator_fit_north_is_above_south():
    fit = MercatorFit([(60.0, 0.0), (-60.0, 0.0)], 800, 600)
    north = fit.to_pixel(60.0, 0.0)
    south = fit.to_pixel(-60.0, 0.0)
    assert north[1] < south[1]                               # north maps to a SMALLER y (higher on screen)


def test_valid_point_rejects_nan_inf_and_junk():
    from src.ui.qt.flock_heatmap_tab import _valid_point
    good = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [11.0, 48.0]}}
    assert _valid_point(good)
    for bad_coords in ([float("nan"), 48.0], [11.0, float("inf")], [True, 48.0], ["x", 1], [1], []):
        assert not _valid_point(
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": bad_coords}})
    for junk in (None, "str", 42, {"geometry": None}, {"geometry": {"type": "LineString", "coordinates": [[0, 0]]}}):
        assert not _valid_point(junk)


def test_widget_nan_coord_does_not_collapse_map(qapp):
    """A NaN coordinate in a saved scan must be FILTERED, not collapse every real camera to the center."""
    w = FlockHeatmapTab()
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [11.0, 48.0]}, "properties": {"count": 1}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [12.0, 49.0]}, "properties": {"count": 1}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [float("nan"), 50.0]}, "properties": {}},
    ]}
    w.set_geojson(gj)
    assert w.camera_count == 2                               # the NaN feature dropped; the two real ones survive


def test_widget_lat_lon_not_swapped(qapp):
    """End-to-end guard against the classic [lon,lat] vs [lat,lon] swap: a NE camera must render to the
    upper-right of a SW camera (larger x, smaller y)."""
    from src.ui.qt.flock_heatmap_tab import MercatorFit
    sw = (40.0, -74.0)   # (lat, lon) ~ New York
    ne = (48.0, 2.0)     # (lat, lon) ~ Paris (north-east of NY)
    fit = MercatorFit([sw, ne], 800, 600)
    psw = fit.to_pixel(*sw)
    pne = fit.to_pixel(*ne)
    assert pne[0] > psw[0]                                   # east -> larger x
    assert pne[1] < psw[1]                                   # north -> smaller y


def test_widget_hostile_json_file_returns_zero(qapp, tmp_path):
    import json as _json
    # valid JSON, but properties:null and a non-numeric count on otherwise-valid Points -> must NOT crash.
    hostile = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [11.0, 48.0]}, "properties": None},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [12.0, 49.0]}, "properties": {"count": "abc"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [13.0, 50.0]}, "properties": {"count": [1, 2]}},
    ]}
    p = tmp_path / "hostile.geojson"
    p.write_text(_json.dumps(hostile), encoding="utf-8")
    w = FlockHeatmapTab()
    n = w.load_geojson_file(str(p))                          # must not raise
    assert n == 3                                            # all three are valid Points; bad counts default to 1
    w.render_native()                                        # and it renders without crashing


def test_widget_json_nan_token_file_is_filtered(qapp, tmp_path):
    # Python json.load accepts the NaN token by default -> the loaded feature must be filtered, not crash.
    p = tmp_path / "nan.geojson"
    p.write_text(
        '{"type":"FeatureCollection","features":['
        '{"type":"Feature","geometry":{"type":"Point","coordinates":[11.0,48.0]},"properties":{"count":1}},'
        '{"type":"Feature","geometry":{"type":"Point","coordinates":[NaN,50.0]},"properties":{}}]}',
        encoding="utf-8")
    w = FlockHeatmapTab()
    assert w.load_geojson_file(str(p)) == 1                  # only the finite camera survives
