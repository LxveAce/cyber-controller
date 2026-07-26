"""OSM/Overpass -> ALPR camera parser (src/core/flock_osm.py), the DeFlock awareness-map import.

The sample below mirrors a REAL Overpass API response (verified 2026-07-26 against overpass-api.de):
top-level `elements` array of `{type:node, id, lat, lon, tags}`, DeFlock ALPR nodes tagged
`man_made=surveillance` + `surveillance:type=ALPR` + `manufacturer`/`operator`. Pure + offline — no
network; the parser takes a dict and emits awareness-only CameraDetection records.
"""
from __future__ import annotations

import json

import pytest

from src.core.flock import CameraDetection
from src.core.flock_osm import (
    OSM_DETECTION_METHOD,
    build_overpass_query,
    cameras_from_overpass,
    fetch_alpr_geojson,
    geojson_from_overpass,
)

# Grounded on a real overpass-api.de response for man_made=surveillance + surveillance:type=ALPR.
SAMPLE = {
    "version": 0.6,
    "generator": "Overpass API",
    "elements": [
        {  # a real Flock Safety ALPR node (manufacturer, no explicit operator)
            "type": "node", "id": 12731641825, "lat": 33.3781454, "lon": -111.9593304,
            "tags": {
                "camera:mount": "pole", "camera:type": "fixed", "direction": "80",
                "man_made": "surveillance", "manufacturer": "Flock Safety",
                "surveillance": "public", "surveillance:type": "ALPR",
            },
        },
        {  # operator present -> operator wins over manufacturer for the label
            "type": "node", "id": 999, "lat": 33.4, "lon": -111.9,
            "tags": {"man_made": "surveillance", "operator": "City PD"},
        },
        {"type": "node", "id": 111, "tags": {"man_made": "surveillance"}},     # no coords -> skip
        {"type": "way", "id": 222, "tags": {"man_made": "surveillance"}},      # not a node -> skip
        {"type": "node", "id": 333, "lat": 1.0, "lon": 2.0, "tags": {"amenity": "cafe"}},  # skip
    ],
}


def test_parses_only_located_surveillance_nodes():
    cams = cameras_from_overpass(SAMPLE)
    assert len(cams) == 2                                   # the two located surveillance nodes
    assert all(isinstance(c, CameraDetection) for c in cams)


def test_camera_fields_grounded_and_awareness_only():
    cam = cameras_from_overpass(SAMPLE)[0]
    assert cam.mac == "osm:12731641825"                    # OSM node id as identity, not a MAC
    assert round(cam.lat, 6) == 33.378145 and round(cam.lon, 6) == -111.959330
    assert cam.ssid == "Flock Safety"                      # manufacturer label
    assert cam.detection_method == OSM_DETECTION_METHOD    # marked DB-imported, not an RF hit
    assert cam.rssi == 0 and cam.channel == 0 and cam.frequency == 0   # a map entry has no RF data


def test_operator_tag_wins_over_manufacturer():
    cam = cameras_from_overpass(SAMPLE)[1]
    assert cam.ssid == "City PD"


def test_geojson_matches_the_existing_camera_feature_shape():
    gj = geojson_from_overpass(SAMPLE)
    assert gj["type"] == "FeatureCollection" and len(gj["features"]) == 2
    feat = gj["features"][0]
    # GeoJSON is [lon, lat] (x, y) — as CameraDetection.to_feature / load_geojson_file use.
    assert feat["geometry"]["coordinates"] == [-111.95933, 33.378145]
    assert feat["properties"]["mac"] == "osm:12731641825"


def test_robust_to_empty_and_garbage_input():
    assert cameras_from_overpass({}) == []
    assert cameras_from_overpass({"elements": []}) == []
    assert cameras_from_overpass({"elements": [None, 7, "x", {}]}) == []
    assert geojson_from_overpass({})["features"] == []


# ── gated fetch: query-builder + user-initiated cached runner (network mocked) ────────────────────

_BBOX = (33.3, -112.2, 33.7, -111.8)   # (south, west, north, east)


def test_build_overpass_query_has_the_grounded_tags_and_bbox():
    q = build_overpass_query(_BBOX, limit=500, timeout=45)
    assert q.startswith("[out:json][timeout:45];")
    assert '"man_made"="surveillance"' in q and '"surveillance:type"="ALPR"' in q
    assert "(33.3,-112.2,33.7,-111.8)" in q       # OverpassQL bbox order: S,W,N,E
    assert q.endswith("out 500;")


def test_build_overpass_query_rejects_a_bad_bbox():
    with pytest.raises(ValueError):
        build_overpass_query((90.0, 0.0, 10.0, 5.0))   # south > north


def test_fetch_uses_the_injected_fetcher_and_parses_to_geojson(tmp_path):
    seen = []

    def fake(url):
        seen.append(url)
        return json.dumps(SAMPLE)

    gj = fetch_alpr_geojson(_BBOX, str(tmp_path / "cams.geojson"), fetcher=fake)
    assert gj["type"] == "FeatureCollection" and len(gj["features"]) == 2   # parsed to cameras
    assert len(seen) == 1 and "man_made" in seen[0]   # one query, carrying the grounded tags


def test_fetch_caches_and_does_not_hammer_the_api(tmp_path):
    cache = str(tmp_path / "cams.geojson")
    fetch_alpr_geojson(_BBOX, cache, fetcher=lambda _url: json.dumps(SAMPLE))   # warms the cache

    def explode(_url):
        raise AssertionError("must NOT re-fetch while the cache is fresh")

    gj = fetch_alpr_geojson(_BBOX, cache, fetcher=explode)   # fresh cache -> no network
    assert len(gj["features"]) == 2   # served from the offline cache
