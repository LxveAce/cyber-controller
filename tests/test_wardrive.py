"""Tests for the wardriving core (src/core/wardrive.py) — pure parsing + WiGLE CSV logic."""

from __future__ import annotations

import io

from src.core import wardrive as wd


def test_dm_to_dd():
    assert round(wd._dm_to_dd("4807.038", "N"), 4) == 48.1173
    assert round(wd._dm_to_dd("01131.000", "E"), 4) == 11.5167
    assert wd._dm_to_dd("4807.038", "S") < 0
    assert wd._dm_to_dd("", "N") is None


def test_parse_nmea_gga_fix():
    f = wd.parse_nmea("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47")
    assert f is not None and f.has_fix
    assert round(f.lat, 3) == 48.117 and round(f.lon, 3) == 11.517 and f.alt == 545.4


def test_parse_nmea_gga_nofix():
    f = wd.parse_nmea("$GPGGA,123519,,,,,0,00,,,M,,M,,*47")
    assert f is not None and not f.has_fix


def test_parse_nmea_rmc():
    ok = wd.parse_nmea("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A")
    assert ok is not None and ok.has_fix
    void = wd.parse_nmea("$GPRMC,123519,V,,,,,,,230394,,*00")
    assert void is not None and not void.has_fix
    assert wd.parse_nmea("not nmea") is None


def test_channel_to_frequency():
    assert wd.channel_to_frequency(1) == 2412
    assert wd.channel_to_frequency(6) == 2437
    assert wd.channel_to_frequency(14) == 2484
    assert wd.channel_to_frequency(36) == 5180
    assert wd.channel_to_frequency(0) == 0


def test_parse_marauder_ap():
    a = wd.parse_marauder_ap("RSSI:-50 Ch:6 BSSID:AA:BB:CC:DD:EE:FF ESSID:MyNet")
    assert a and a.bssid == "aa:bb:cc:dd:ee:ff" and a.rssi == -50 and a.channel == 6 and a.ssid == "MyNet"
    b = wd.parse_marauder_ap("0) BSSID: 11:22:33:44:55:66 | RSSI: -67 | Ch: 11 | WPA2 | ESSID: HomeNet")
    assert b and b.rssi == -67 and b.channel == 11 and b.ssid == "HomeNet" and "WPA2" in b.auth
    assert wd.parse_marauder_ap("no mac here") is None


def test_to_wigle_row():
    obs = wd.ApObservation(bssid="aa:bb:cc:dd:ee:ff", ssid="Net", channel=6, rssi=-40, auth="[WPA2][ESS]")
    fix = wd.GpsFix(lat=48.1173, lon=11.5167, alt=545.4, has_fix=True)
    row = wd.to_wigle_row(obs, fix, "2026-06-27 12:00:00")
    cols = row.split(",")
    assert cols[0] == "AA:BB:CC:DD:EE:FF" and cols[1] == "Net"
    assert cols[4] == "6" and cols[5] == "2437" and cols[6] == "-40"
    assert cols[7] == "48.117300" and cols[13] == "WIFI"


def test_session_gating_and_dedup():
    buf = io.StringIO()
    s = wd.WardriveSession(buf)
    s.start()
    text = buf.getvalue()
    assert text.splitlines()[0].startswith("WigleWifi-1.6")
    assert text.splitlines()[1] == wd.WIGLE_HEADER

    # no fix yet -> no row
    assert s.observe("BSSID:AA:BB:CC:DD:EE:FF RSSI:-40 Ch:1 ESSID:Net") is False
    assert s.ap_count == 0

    s.update_gps("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47")
    assert s.has_fix
    assert s.observe("BSSID:AA:BB:CC:DD:EE:FF RSSI:-40 Ch:1 ESSID:Net", now="2026-06-27 00:00:00") is True
    assert s.ap_count == 1
    # weaker RSSI for same BSSID -> not rewritten
    assert s.observe("BSSID:AA:BB:CC:DD:EE:FF RSSI:-60 Ch:1 ESSID:Net", now="2026-06-27 00:00:01") is False
    assert s.ap_count == 1
    # stronger RSSI -> rewritten, count unchanged
    assert s.observe("BSSID:AA:BB:CC:DD:EE:FF RSSI:-30 Ch:1 ESSID:Net", now="2026-06-27 00:00:02") is True
    assert s.ap_count == 1
    # new BSSID -> new row
    assert s.observe("BSSID:11:22:33:44:55:66 RSSI:-50 Ch:6 ESSID:Net2", now="2026-06-27 00:00:03") is True
    assert s.ap_count == 2

    data_rows = [ln for ln in buf.getvalue().splitlines()[2:] if ln]
    assert all(len(r.split(",")) == 14 for r in data_rows)
