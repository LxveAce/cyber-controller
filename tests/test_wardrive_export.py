"""Tests for the wardrive KML/GPX/GeoJSON exporters (WS6) — src/core/wardrive.py.

Covers: one map feature per network, well-formed XML (parsed with defusedxml) + valid GeoJSON, the
XML-escaping of attacker-controlled SSID/BSSID (the XML analog of the CSV-injection export rule) and
the GeoJSON json-escaping equivalent, the empty-input case, and the ``--wardrive-kml`` /
``--wardrive-gpx`` / ``--wardrive-geojson`` CLI wrapper.
"""
from __future__ import annotations

import json

import defusedxml.ElementTree as ET

from src.core.wardrive import (
    wardrive_export_cli,
    wigle_csv_to_geojson,
    wigle_csv_to_gpx,
    wigle_csv_to_kml,
)

_KML_NS = "{http://www.opengis.net/kml/2.2}"
_GPX_NS = "{http://www.topografix.com/GPX/1/1}"
_HEADER = ("MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,"
           "CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type")


def _csv(*rows: str) -> str:
    return "WigleWifi-1.6,appRelease=1\n" + _HEADER + "\n" + "\n".join(rows) + "\n"


_TWO = _csv(
    "AA:BB:CC:DD:EE:FF,HomeNet,[WPA2],2026-08-09 12:00:00,6,2437,-50,37.7749,-122.4194,10,5,WIFI",
    "11:22:33:44:55:66,,[OPEN],2026-08-09 12:01:00,1,2412,-70,37.7750,-122.4195,10,5,WIFI",
)


def test_kml_is_valid_xml_one_placemark_per_network():
    root = ET.fromstring(wigle_csv_to_kml(_TWO))
    assert len(root.findall(f".//{_KML_NS}Placemark")) == 2


def test_gpx_is_valid_xml_one_waypoint_per_network():
    root = ET.fromstring(wigle_csv_to_gpx(_TWO))
    assert len(root.findall(f".//{_GPX_NS}wpt")) == 2


def test_hidden_ssid_is_labelled_not_blank():
    # The second row has no SSID; it should render as "(hidden)", not an empty name.
    kml = wigle_csv_to_kml(_TWO)
    assert "(hidden)" in kml


def test_ssid_xml_injection_is_escaped():
    evil = _csv(
        'AA:BB:CC:DD:EE:01,<x>&"pwn,[WPA2],2026-08-09 12:00:00,6,2437,-50,37.7,-122.4,0,5,WIFI',
    )
    for doc in (wigle_csv_to_kml(evil), wigle_csv_to_gpx(evil)):
        # The raw angle brackets must never reach the output verbatim...
        assert "<x>" not in doc
        assert "&lt;x&gt;" in doc
        # ...and the document must still parse, with the name round-tripping to the real SSID.
        root = ET.fromstring(doc)
        names = [e.text for e in root.iter() if e.tag.endswith("name")]
        assert '<x>&"pwn' in names


def test_control_chars_in_ssid_stay_valid_xml():
    # A raw NUL/control char in an SSID is illegal in XML 1.0 even after escaping; it must be
    # stripped so the document still parses (escape() alone would not save it).
    nasty = _csv(
        "AA:BB:CC:DD:EE:02,ev\x00il\x07net,[WPA2],t,6,2437,-50,37.7,-122.4,0,5,WIFI",
    )
    for doc in (wigle_csv_to_kml(nasty), wigle_csv_to_gpx(nasty)):
        root = ET.fromstring(doc)  # must not raise
        names = [e.text for e in root.iter() if e.tag.endswith("name")]
        assert "evilnet" in names  # the two control chars were dropped, the rest kept


def test_noncharacters_in_ssid_stay_valid_xml():
    # U+FFFE / U+FFFF are valid UTF-8 but forbidden by XML 1.0's Char production, so >=0x20 alone is
    # not enough — they must be dropped or it won't parse. (Regression: adversarial finding.)
    for bad in ("ev￿il", "ev￾il"):
        row = f"AA:BB:CC:DD:EE:03,{bad},[WPA2],t,6,2437,-50,37.7,-122.4,0,5,WIFI"
        for doc in (wigle_csv_to_kml(_csv(row)), wigle_csv_to_gpx(_csv(row))):
            root = ET.fromstring(doc)  # must not raise
            assert "evil" in [e.text for e in root.iter() if e.tag.endswith("name")]


def test_all_control_char_ssid_renders_hidden_not_empty():
    # An SSID that strips to nothing must fall back to "(hidden)", not an empty <name></name>.
    row = "AA:BB:CC:DD:EE:04,\x01\x02,[WPA2],t,6,2437,-50,37.7,-122.4,0,5,WIFI"
    for doc in (wigle_csv_to_kml(_csv(row)), wigle_csv_to_gpx(_csv(row))):
        assert "<name></name>" not in doc
        assert "(hidden)" in doc


def test_empty_csv_is_valid_empty_doc():
    empty = _csv()  # pre-header + column header, no data rows
    for fn, ns, tag in ((wigle_csv_to_kml, _KML_NS, "Placemark"),
                        (wigle_csv_to_gpx, _GPX_NS, "wpt")):
        root = ET.fromstring(fn(empty))
        assert root.findall(f".//{ns}{tag}") == []


def test_geojson_is_valid_featurecollection_one_feature_per_network():
    fc = json.loads(wigle_csv_to_geojson(_TWO))
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 2
    f = fc["features"][0]
    assert f["type"] == "Feature"
    assert f["geometry"]["type"] == "Point"
    # RFC 7946: coordinates are [lon, lat] — longitude first.
    lon, lat = f["geometry"]["coordinates"]
    assert -123 < lon < -122 and 37 < lat < 38
    assert set(f["properties"]) == {"ssid", "bssid"}


def test_geojson_is_deterministic_sorted_by_bssid():
    order = [f["properties"]["bssid"] for f in json.loads(wigle_csv_to_geojson(_TWO))["features"]]
    assert order == sorted(order)


def test_geojson_string_injection_round_trips_safely():
    # A quote/backslash/markup-laden SSID is data, not markup, inside JSON — it must survive as the
    # exact string with no escape breakout (json.dumps handles it; no XML-injection surface here).
    evil = _csv(
        'AA:BB:CC:DD:EE:07,a"\\</script>b,[WPA2],t,6,2437,-50,37.7,-122.4,0,5,WIFI',
    )
    fc = json.loads(wigle_csv_to_geojson(evil))  # must parse
    assert fc["features"][0]["properties"]["ssid"] == 'a"\\</script>b'


def test_geojson_empty_csv_is_empty_featurecollection():
    fc = json.loads(wigle_csv_to_geojson(_csv()))
    assert fc == {"type": "FeatureCollection", "features": []}


def test_export_cli_missing_file_returns_1(capsys):
    assert wardrive_export_cli("does-not-exist.csv", "kml") == 1
    assert "no such file" in capsys.readouterr().out


def test_export_cli_unknown_format_returns_1(tmp_path, capsys):
    p = tmp_path / "wd.csv"
    p.write_text(_TWO, encoding="utf-8")
    assert wardrive_export_cli(str(p), "shapefile") == 1
    assert "unknown export format" in capsys.readouterr().out


def test_export_cli_writes_valid_kml_and_gpx(tmp_path, capsys):
    p = tmp_path / "wd.csv"
    p.write_text(_TWO, encoding="utf-8")
    assert wardrive_export_cli(str(p), "kml") == 0
    assert len(ET.fromstring(capsys.readouterr().out).findall(f".//{_KML_NS}Placemark")) == 2
    assert wardrive_export_cli(str(p), "gpx") == 0
    assert len(ET.fromstring(capsys.readouterr().out).findall(f".//{_GPX_NS}wpt")) == 2
    assert wardrive_export_cli(str(p), "geojson") == 0
    assert len(json.loads(capsys.readouterr().out)["features"]) == 2
