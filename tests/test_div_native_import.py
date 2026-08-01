"""ESP32-DIV native wardrive SD-log import -- div_native_to_points + the sniff route (grounded).

DIV's persistent on-SD log is its OWN CSV (epoch_ms,... -- cifertech/ESP32-DIV gps.cpp:3447/3659,
detected by the firmware's own strncmp(line,"epoch_ms",8)), NOT WiGLE (its WiGLE file is a transient
SD.remove'd upload temp). Before this, wardrive_import.py:87 dropped 100% of a real DIV SD file --
"No mappable points". Grounded vs the real firmware source. Pure + tolerant; header-name resolution.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.core import wardrive_import as wi

_HEADER = "epoch_ms,utc,date,lat,lon,alt_m,fix,sats,hdop,radio,ssid,bssid,ch,rssi_dbm,auth"
_ROW1 = ("1699999999000,12:00:00,2026-08-01,47.6062,-122.3321,50.0,1,8,1.2,WIFI,HomeNet,"
         "AA:BB:CC:DD:EE:01,6,-55,WPA2")
_ROW2 = ("1699999999500,12:00:01,2026-08-01,40.10,-74.20,10.0,1,9,0.9,WIFI,OtherNet,"
         "AA:BB:CC:DD:EE:02,11,-60,OPEN")


def _log(*rows):
    return "\n".join([_HEADER, *rows]) + "\n"


def test_sniff_detects_div_native():
    assert wi.sniff_wardrive_format(_log(_ROW1)) == "div_native"
    # a WiGLE CSV must NOT be mis-sniffed as div_native
    assert wi.sniff_wardrive_format("WigleWifi-1.6\nMAC,SSID\nAA:BB:CC:DD:EE:01,x") == "wigle"


def test_div_native_parses_points():
    pts = wi.div_native_to_points(_log(_ROW1, _ROW2))
    assert len(pts) == 2
    assert (47.6062, -122.3321, "HomeNet", "AA:BB:CC:DD:EE:01") in pts
    assert (40.10, -74.20, "OtherNet", "AA:BB:CC:DD:EE:02") in pts


def test_wardrive_points_routes_div_native():
    # the one-entry dispatcher must route a DIV-native log to the native reader (was dropped -> 0)
    assert len(wi.wardrive_points(_log(_ROW1, _ROW2))) == 2


def test_columns_resolved_by_header_name_not_index():
    # drift-tolerant: a reordered header still resolves lat/lon/ssid/bssid by NAME
    hdr = "epoch_ms,bssid,ssid,lat,lon,ch"
    row = "1699999999000,AA:BB:CC:DD:EE:03,DriftNet,51.50,-0.12,6"
    assert wi.div_native_to_points(hdr + "\n" + row + "\n") == [
        (51.50, -0.12, "DriftNet", "AA:BB:CC:DD:EE:03")]


def test_guards_drop_bad_rows():
    bad = _log(
        "1,t,d,0.0,0.0,0,1,8,1,WIFI,NullIsland,AA:BB:CC:DD:EE:04,6,-55,WPA2",       # 0/0 no-fix
        "1,t,d,999.0,10.0,0,1,8,1,WIFI,OOR,AA:BB:CC:DD:EE:05,6,-55,WPA2",     # lat out of range
        "1,t,d,47.6,-122.3,0,1,8,1,WIFI,NoMac,NOTAMAC,6,-55,WPA2",                  # bad BSSID
        _ROW1,                                                               # the one good row
    )
    assert wi.div_native_to_points(bad) == [(47.6062, -122.3321, "HomeNet", "AA:BB:CC:DD:EE:01")]


def test_tolerant_on_junk():
    assert wi.div_native_to_points("") == []
    assert wi.div_native_to_points("not,a,div,log\n1,2,3") == []   # no epoch_ms header -> nothing


def test_dedup_by_bssid():
    assert len(wi.div_native_to_points(_log(_ROW1, _ROW1))) == 1   # same BSSID twice -> one point

# End-to-end (a DIV file through the map UI) is covered by composition: the dispatch is pinned above
# (test_wardrive_points_routes_div_native) + the load_wardrive_log->wardrive_points wiring in
# test_flow_slice_d / test_kismet_import_ui. A standalone Qt-widget test here segfaults on isolated
# QApplication teardown (the known Qt-teardown flake), so it's intentionally not duplicated.
