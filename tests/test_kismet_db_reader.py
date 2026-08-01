"""Kismet ``.kismet`` (SQLite) reader — `wardrive_import.kismet_db_to_points`.

Grounded on the documented kismetdb schema (research wf_0c7aafbc): the ``devices`` table (one row per
device, ``devmac`` = AP BSSID, ``avg_lat``/``avg_lon`` the map point, ``phyname``/``type`` for filtering, a
JSON ``device`` blob holding the SSID) + the v4→v5 coordinate encoding pivot (int×100000 → REAL doubles).

These tests build SYNTHETIC .kismet DBs from that schema. They prove the reader's LOGIC (filtering, the
version-drift decode, the SSID key-path fallbacks, the tolerant guards) — they do NOT prove it reads a REAL
Kismet capture (KISMET_READER_HW_VERIFIED is False; a real GPS-tagged log from the operator's build is the
arbiter — the type/phyname strings + SSID nesting could differ in the field).
"""
from __future__ import annotations

import json
import sqlite3

from src.core.wardrive_import import KISMET_READER_HW_VERIFIED, kismet_db_to_points

_DEVICES_DDL = """
CREATE TABLE KISMET (kismet_version TEXT, db_version INT, db_module TEXT);
CREATE TABLE devices (
    first_time INT, last_time INT, devkey TEXT, phyname TEXT, devmac TEXT,
    strongest_signal INT, min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL,
    avg_lat REAL, avg_lon REAL, bytes_data INT, type TEXT, device BLOB,
    UNIQUE(phyname, devmac) ON CONFLICT REPLACE
);
"""

# SSID JSON shapes the reader must all resolve (newer nested / older flat / the advertised-ssid map).
_SSID_NESTED = {"dot11.device": {"dot11.device.last_beaconed_ssid_record": {"dot11.advertisedssid.ssid": "HomeNet-5G"}}}
_SSID_FLAT = {"dot11.device": {"dot11.device.last_beaconed_ssid": "OldNet"}}
_SSID_MAP = {"dot11.device": {"dot11.device.advertised_ssid_map": [{"dot11.advertisedssid.ssid": "MapNet"}]}}


def _make_kismet(path, db_version, rows):
    """rows = [(devmac, phyname, type, avg_lat, avg_lon, device_dict_or_None)]. Coords are written as
    given, so a v4 fixture passes pre-scaled ints (degrees×100000) to exercise the decode."""
    con = sqlite3.connect(str(path))
    con.executescript(_DEVICES_DDL)
    con.execute("INSERT INTO KISMET VALUES (?,?,?)", ("synthetic", db_version, "kismetlog"))
    for mac, phy, typ, lat, lon, dev in rows:
        blob = json.dumps(dev) if dev is not None else None
        con.execute(
            "INSERT INTO devices (first_time,last_time,devkey,phyname,devmac,strongest_signal,"
            "min_lat,min_lon,max_lat,max_lon,avg_lat,avg_lon,bytes_data,type,device) "
            "VALUES (0,0,?,?,?,-40,?,?,?,?,?,?,0,?,?)",
            (mac.replace(":", ""), phy, mac, lat, lon, lat, lon, lat, lon, typ, blob))
    con.commit()
    con.close()


def test_reads_wifi_aps_with_gps_and_all_ssid_shapes(tmp_path):
    p = tmp_path / "v8.kismet"
    _make_kismet(p, 8, [
        ("AA:BB:CC:DD:EE:01", "IEEE802.11", "Wi-Fi AP", 47.6062, -122.3321, _SSID_NESTED),
        ("AA:BB:CC:DD:EE:02", "IEEE802.11", "Wi-Fi AP", 40.10, -74.20, _SSID_FLAT),
        ("AA:BB:CC:DD:EE:03", "IEEE802.11", "Wi-Fi AP", 51.50, -0.12, _SSID_MAP),
        ("AA:BB:CC:DD:EE:04", "IEEE802.11", "Wi-Fi AP", 0.0, 0.0, _SSID_NESTED),      # no-fix -> drop
        ("AA:BB:CC:DD:EE:05", "IEEE802.11", "Wi-Fi Client", 12.0, 34.0, None),         # not an AP -> drop
        ("BB:BB:BB:BB:BB:01", "Bluetooth", "BTLE Device", 12.0, 34.0, None),           # wrong phy -> drop
    ])
    by = {b: (lat, lon, s) for (lat, lon, s, b) in kismet_db_to_points(str(p))}
    assert set(by) == {"AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02", "AA:BB:CC:DD:EE:03"}   # 3 positioned APs
    assert by["AA:BB:CC:DD:EE:01"] == (47.6062, -122.3321, "HomeNet-5G")   # nested SSID + no lat/lon swap
    assert by["AA:BB:CC:DD:EE:02"][2] == "OldNet"                          # flat SSID form
    assert by["AA:BB:CC:DD:EE:03"][2] == "MapNet"                          # advertised_ssid_map form


def test_v4_decodes_degrees_times_100000(tmp_path):
    # kismetdb v4 stores avg_lat/avg_lon as degrees×100000 integers; the reader must divide by 1e5.
    p = tmp_path / "v4.kismet"
    _make_kismet(p, 4, [
        ("AA:BB:CC:DD:EE:01", "IEEE802.11", "Wi-Fi AP", 4760620, -12233210, _SSID_NESTED),
    ])
    pts = kismet_db_to_points(str(p))
    assert pts == [(47.6062, -122.3321, "HomeNet-5G", "AA:BB:CC:DD:EE:01")]


def test_corrupt_json_keeps_the_point_but_blanks_the_name(tmp_path):
    p = tmp_path / "badjson.kismet"
    con = sqlite3.connect(str(p))
    con.executescript(_DEVICES_DDL)
    con.execute("INSERT INTO KISMET VALUES ('x',8,'k')")
    con.execute(
        "INSERT INTO devices (phyname,devmac,avg_lat,avg_lon,type,device) VALUES (?,?,?,?,?,?)",
        ("IEEE802.11", "AA:BB:CC:DD:EE:07", 10.0, 20.0, "Wi-Fi AP", "{not valid json"))
    con.commit()
    con.close()
    assert kismet_db_to_points(str(p)) == [(10.0, 20.0, "", "AA:BB:CC:DD:EE:07")]


def test_tolerant_on_non_kismet_missing_and_empty(tmp_path):
    # A non-Kismet SQLite (no devices table) -> [].
    other = tmp_path / "other.sqlite"
    con = sqlite3.connect(str(other))
    con.execute("CREATE TABLE t (x INT)")
    con.commit()
    con.close()
    assert kismet_db_to_points(str(other)) == []
    # A missing file and a non-SQLite blob -> [] (never raises).
    assert kismet_db_to_points(str(tmp_path / "nope.kismet")) == []
    (tmp_path / "junk.kismet").write_bytes(b"not a sqlite database" * 100)
    assert kismet_db_to_points(str(tmp_path / "junk.kismet")) == []
    # KISMET present but devices empty -> [].
    empty = tmp_path / "empty.kismet"
    _make_kismet(empty, 8, [])
    assert kismet_db_to_points(str(empty)) == []


def test_reader_is_flagged_hw_unverified():
    # The honesty gate: this reader is NOT confirmed against a real .kismet log. It must stay False until
    # a real GPS-tagged capture from the operator's Kismet build confirms the decode + SSID nesting.
    assert KISMET_READER_HW_VERIFIED is False
