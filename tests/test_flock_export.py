"""cameras_geojson_to_csv — export a located-camera GeoJSON as spreadsheet-friendly, injection-safe CSV."""
from __future__ import annotations

from src.core import flock

_HEADER = "mac,lat,lon,ssid,oui,detection_method,rssi,channel,frequency,utc,first_seen,last_seen,count"


def _fc(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def _cam(mac, lon, lat, **props):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"mac": mac, **props}}


def test_csv_header_and_one_row_per_camera():
    gj = _fc(
        _cam("aa:bb:cc:dd:ee:ff", 11.5, 48.1, ssid="CamNet", rssi=-40, channel=6, count=3,
             first_seen="2026-06-27 00:00:00", last_seen="2026-06-27 00:01:00"),
        _cam("11:22:33:44:55:66", -0.12, 51.5, ssid="Flock2", rssi=-70, channel=11, count=1),
    )
    lines = flock.cameras_geojson_to_csv(gj).strip().splitlines()
    assert lines[0] == _HEADER
    assert len(lines) == 3  # header + 2 cameras
    # lat/lon are lifted out of the GeoJSON [lon, lat] geometry and formatted to 6 dp
    assert lines[1].startswith("aa:bb:cc:dd:ee:ff,48.100000,11.500000,CamNet,")
    assert ",-40," in lines[1]        # a legit NEGATIVE rssi stays a plain number (not quote-prefixed)
    assert lines[1].endswith(",3")    # count is the final column


def test_csv_neutralizes_formula_injection_in_ssid():
    # an attacker-chosen SSID beginning with a spreadsheet formula trigger must be de-fanged, not emitted raw
    gj = _fc(_cam("aa:bb:cc:dd:ee:ff", 11.5, 48.1, ssid="=cmd|'/c calc'!A1", rssi=-50))
    row = flock.cameras_geojson_to_csv(gj).strip().splitlines()[1]
    assert ",'=cmd" in row              # the ssid cell is prefixed with a single quote
    assert ",=cmd" not in row           # never the raw formula


def test_csv_skips_features_without_a_location():
    gj = _fc(
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": []}, "properties": {"mac": "x"}},
        {"type": "Feature", "properties": {"mac": "y"}},   # no geometry at all
        "not-a-feature",
        _cam("aa:bb:cc:dd:ee:ff", 11.5, 48.1, ssid="Ok"),
    )
    assert len(flock.cameras_geojson_to_csv(gj).strip().splitlines()) == 2  # header + the ONE valid camera


def test_csv_empty_and_malformed_are_header_only():
    assert flock.cameras_geojson_to_csv(_fc()).strip() == _HEADER
    assert flock.cameras_geojson_to_csv({}).strip() == _HEADER  # tolerates a totally malformed input
