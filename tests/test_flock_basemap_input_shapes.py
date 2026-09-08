"""basemap_paths() input-shape hardening — the projection must skip malformed-but-benign GeoJSON container
shapes (non-list ``features``, non-dict feature/geometry, scalar coordinate containers) instead of raising,
while leaving valid Polygon/MultiPolygon projections unchanged and preserving valid siblings.

Pure + headless: only the projection helpers are imported (no widget, no display). Fed synthetic local
structures, never a hostile payload. Filename has no GUI-toolkit-fixture marker, so the hosted CI text glob
includes it.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from src.ui.qt.flock_heatmap_tab import basemap_paths, world_px

# A valid closed triangle ring, GeoJSON [lon, lat] order.
_TRI = [[0, 0], [10, 0], [10, 10], [0, 0]]
_EXPECT = [world_px(lat, lon) for lon, lat in _TRI]


def _fc(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def _poly(coords):
    return {"geometry": {"type": "Polygon", "coordinates": coords}}


# ── valid projections are unchanged ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("gj", [
    _fc({"geometry": {"type": "Polygon", "coordinates": [_TRI]}}),
    _fc({"geometry": {"type": "MultiPolygon", "coordinates": [[_TRI]]}}),
])
def test_valid_polygon_and_multipolygon_project_to_world_px_unchanged(gj):
    assert basemap_paths(gj) == [_EXPECT]


# ── malformed container shapes are skipped, never raised ──────────────────────────────────────────

@pytest.mark.parametrize("gj", [
    pytest.param({"type": "FeatureCollection", "features": {"a": 1}}, id="features-is-dict"),
    pytest.param({"type": "FeatureCollection", "features": 5}, id="features-is-int"),
    pytest.param({"type": "FeatureCollection", "features": None}, id="features-is-none"),
    pytest.param("not a mapping", id="geojson-not-dict"),
    pytest.param({}, id="geojson-missing-features"),
])
def test_non_container_top_level_yields_no_rings_without_raising(gj):
    assert basemap_paths(gj) == []


@pytest.mark.parametrize("bad_feature", [
    pytest.param("not a dict", id="feature-str"),
    pytest.param(5, id="feature-int"),
    pytest.param(None, id="feature-none"),
    pytest.param(["a", "list"], id="feature-list"),
])
def test_non_dict_feature_is_skipped_valid_sibling_survives(bad_feature):
    gj = _fc(bad_feature, _poly([_TRI]))
    assert basemap_paths(gj) == [_EXPECT]


@pytest.mark.parametrize("bad_geom", [
    pytest.param("Polygon", id="geometry-str"),
    pytest.param(5, id="geometry-int"),
    pytest.param(["x"], id="geometry-list"),
    pytest.param(None, id="geometry-none"),
])
def test_non_dict_geometry_is_skipped_valid_sibling_survives(bad_geom):
    gj = _fc({"geometry": bad_geom}, _poly([_TRI]))
    assert basemap_paths(gj) == [_EXPECT]


@pytest.mark.parametrize("bad", [
    pytest.param(_fc({"geometry": {"type": "Polygon", "coordinates": 5}}), id="polygon-scalar-coords"),
    pytest.param(_fc({"geometry": {"type": "Polygon", "coordinates": [5]}}), id="polygon-scalar-ring"),
    pytest.param(_fc({"geometry": {"type": "MultiPolygon", "coordinates": 5}}), id="mp-scalar-coords"),
    pytest.param(_fc({"geometry": {"type": "MultiPolygon", "coordinates": [5]}}), id="mp-scalar-polygon"),
    pytest.param(_fc({"geometry": {"type": "Polygon", "coordinates": [[5]]}}), id="polygon-scalar-vertex"),
])
def test_scalar_coordinate_containers_are_skipped_without_raising(bad):
    # The malformed geometry alone yields nothing; no exception escapes the parser boundary.
    assert basemap_paths(bad) == []
    # And it does not poison a following valid sibling.
    poisoned = dict(bad)
    poisoned["features"] = [*bad["features"], _poly([_TRI])]
    assert basemap_paths(poisoned) == [_EXPECT]


def test_mixed_bag_preserves_every_valid_sibling_in_order():
    gj = _fc(
        "junk",                                                        # non-dict feature
        {"geometry": {"type": "Polygon", "coordinates": 5}},           # scalar coords
        _poly([_TRI]),                                                 # valid #1
        {"geometry": "Polygon"},                                       # non-dict geometry
        {"geometry": {"type": "MultiPolygon", "coordinates": [[_TRI]]}},  # valid #2
    )
    assert basemap_paths(gj) == [_EXPECT, _EXPECT]


def test_unknown_geometry_type_still_skipped():
    assert basemap_paths(_fc({"geometry": {"type": "LineString", "coordinates": [_TRI]}})) == []
