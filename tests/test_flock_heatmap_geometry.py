"""Flock heatmap (FL F4) — web-mercator projection/geometry core (pure, no Qt widget).

Split out of tests/test_flock_heatmap.py so the hosted runner collects it: the projection/heat/zoom/
cull helpers are unit-tested with no Qt GUI fixture and no offscreen widget (its sibling keeps the
offscreen widget tests). Fed by the same src.ui.qt.flock_heatmap_tab helpers.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from src.ui.qt.flock_heatmap_tab import (
    MercatorFit,
    basemap_paths,
    clamped_zoom_factor,
    dots_in_rect,
    heat_color,
    load_world_basemap,
    web_mercator,
    world_px,
    world_px_inv,
    zoom_step,
)


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


def test_world_px_shared_global_plane():
    W = 1000.0
    assert world_px(0.0, 0.0, W) == (500.0, 500.0)           # equator/prime-meridian -> center of the plane
    assert abs(world_px(0.0, 180.0, W)[0] - 1000.0) < 1e-9   # antimeridian east -> x = world
    assert abs(world_px(0.0, -180.0, W)[0] - 0.0) < 1e-9     # west -> x = 0
    assert world_px(45.0, 0.0, W)[1] < 500.0                 # north -> smaller y (screen-up)
    assert world_px(-45.0, 0.0, W)[1] > 500.0                # south -> larger y
    # same lat -> same y; more-east -> larger x  (a real map plane, not a per-camera fit)
    assert world_px(10.0, 20.0, W)[1] == pytest.approx(world_px(10.0, 60.0, W)[1])
    assert world_px(10.0, 20.0, W)[0] < world_px(10.0, 60.0, W)[0]
    # default world scale is Earth's equatorial circumference in metres (scene units ~= metres)
    assert world_px(0.0, 0.0)[0] == pytest.approx(40_075_016.0 / 2)


def test_world_px_inv_round_trips():
    # the inverse the OSM-import view-bbox reads: world_px -> world_px_inv must recover (lat, lon).
    W = 1000.0
    for lat, lon in [(0.0, 0.0), (45.0, -73.0), (-33.9, 18.4), (51.5, -0.12), (33.45, -111.9)]:
        x, y = world_px(lat, lon, W)
        rlat, rlon = world_px_inv(x, y, W)
        assert rlat == pytest.approx(lat, abs=1e-6) and rlon == pytest.approx(lon, abs=1e-6)


def test_basemap_paths_projects_rings_into_world_plane():
    # a tiny two-country FeatureCollection (one Polygon, one MultiPolygon) -> both rings projected
    gj = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [
                [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]]}},
            {"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": [
                [[[20.0, 20.0], [30.0, 20.0], [30.0, 30.0], [20.0, 20.0]]]]}},
        ],
    }
    rings = basemap_paths(gj, 1000.0)
    assert len(rings) == 2                                   # one ring per polygon
    assert all(len(r) >= 3 for r in rings)                   # closed rings survive
    # each vertex is a projected (x, y) in the shared plane; equator/prime-meridian point -> center
    assert rings[0][0] == pytest.approx(world_px(0.0, 0.0, 1000.0))
    # every projected point lies inside the world square
    for ring in rings:
        for x, y in ring:
            assert 0.0 <= x <= 1000.0 and 0.0 <= y <= 1000.0


def test_basemap_paths_skips_junk_and_short_rings():
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1.0, 2.0]}},   # not a polygon
        {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [
            [[0.0, 0.0], [1.0, 1.0]]]}},                                                  # <3 pts -> dropped
        {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [
            [[0.0, 0.0], [float("nan"), 1.0], [2.0, 2.0], [3.0, 3.0]]]}},                 # one bad vertex skipped
        None,                                                                            # hostile null feature
    ]}
    rings = basemap_paths(gj, 1000.0)
    assert len(rings) == 1                                   # only the last polygon yields a usable ring
    assert len(rings[0]) == 3                                # the NaN vertex was dropped, 3 remain


def test_basemap_paths_empty_on_bad_input():
    assert basemap_paths({}, 1000.0) == []
    assert basemap_paths({"features": None}, 1000.0) == []
    assert basemap_paths("not a dict", 1000.0) == []


def test_load_world_basemap_bundle_present():
    # the bundled Natural Earth 110m world basemap must ship + parse into projectable rings
    gj = load_world_basemap()
    assert gj.get("type") == "FeatureCollection"
    assert len(gj.get("features", [])) > 100                 # ~177 countries
    rings = basemap_paths(gj)
    assert len(rings) > 100                                  # every country contributes at least one ring


def test_zoom_step_notches():
    assert zoom_step(0) == 1.0                                # no wheel movement -> no zoom
    assert abs(zoom_step(120) - 1.2) < 1e-9                   # one notch up -> zoom in by base
    assert abs(zoom_step(-120) - 1.0 / 1.2) < 1e-9           # one notch down -> zoom out
    assert abs(zoom_step(240) - 1.2 ** 2) < 1e-9             # two notches compound
    assert zoom_step(120) * zoom_step(-120) == pytest.approx(1.0)  # in then out -> identity


def test_clamped_zoom_factor_in_band_passes_through():
    # comfortably inside [MIN,MAX]: both directions apply unchanged
    assert clamped_zoom_factor(1.0, 1.2, 0.15, 60.0) == 1.2
    assert clamped_zoom_factor(1.0, 1.0 / 1.2, 0.15, 60.0) == 1.0 / 1.2


def test_clamped_zoom_factor_blocks_only_further_past_a_limit():
    # already at/above MAX -> zoom-IN blocked; zoom-OUT still allowed (can come back down)
    assert clamped_zoom_factor(60.0, 1.2, 0.15, 60.0) == 1.0
    assert clamped_zoom_factor(60.0, 1.0 / 1.2, 0.15, 60.0) == 1.0 / 1.2
    # already at/below MIN -> zoom-OUT blocked; zoom-IN still allowed (can climb back up)
    assert clamped_zoom_factor(0.15, 1.0 / 1.2, 0.15, 60.0) == 1.0
    assert clamped_zoom_factor(0.15, 1.2, 0.15, 60.0) == 1.2


def test_clamped_zoom_factor_not_trapped_below_min():
    # THE BUG: a wide camera set / the world basemap fits at a scale FAR below MIN. The old clamp rejected
    # any out-of-band RESULT, so zoom-in (0.05*1.2=0.06, still < MIN) was blocked too -> "can't scroll to
    # zoom". The fix must let a below-MIN view zoom IN (toward the band) while still blocking zoom-OUT.
    assert clamped_zoom_factor(0.05, 1.2, 0.15, 60.0) == 1.2         # zoom in: allowed (was blocked)
    assert clamped_zoom_factor(0.05, 1.0 / 1.2, 0.15, 60.0) == 1.0   # zoom out: blocked (would go further out)


def test_dots_in_rect_returns_only_intersecting():
    # The viewport cull that _CameraLayer.paint() uses: only dots whose bbox meets the rect are drawn.
    dots = [
        (0.0, 0.0, 1.0),        # 0: inside
        (100.0, 100.0, 1.0),    # 1: far off to the lower-right -> culled
        (10.0, 0.0, 2.0),       # 2: bbox x∈[8,12] straddles the right edge (10) -> kept
        (0.0, -50.0, 1.0),      # 3: well above the top edge -> culled
    ]
    assert dots_in_rect(dots, -5.0, -5.0, 10.0, 10.0) == [0, 2]


def test_dots_in_rect_small_viewport_over_a_dense_set():
    # A tiny viewport over a big grid returns only the handful in view — this is the CPU/RAM win: thousands
    # of off-screen cameras are never drawn.
    dots = [(float(i), float(j), 0.4) for i in range(50) for j in range(50)]   # 2500 dots on a 50x50 grid
    vis = dots_in_rect(dots, 0.0, 0.0, 2.0, 2.0)     # a 2x2 window at the corner
    assert 0 < len(vis) < 20 and len(dots) == 2500   # only the corner few, not all 2500
