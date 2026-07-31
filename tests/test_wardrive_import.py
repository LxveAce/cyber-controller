"""The shared header-aware WiGLE reader (`wardrive_import.iter_wigle_rows`) + the fix it delivers:
CC now reads WigleWifi-1.4 (11-col) and Kismet .wiglecsv, not just the 14-col-1.6 form.

Before this, all three readers hard-required `len(row) >= 14`, so every row of an 11-column 1.4 file
was silently DROPPED (and 1.6 inserts Frequency at index 5, shifting lat/lon, so a fixed-index parser
mis-reads the other version). The reader maps fields by the column-HEADER name; with no header it falls
back to 1.6 positions (what the callers assumed before). Pure, Qt-free, headless.
"""
from __future__ import annotations

from src.core.wardrive import summarize_wigle_csv, wigle_csv_to_points
from src.core.wardrive_import import iter_wigle_rows, netxml_to_points
from src.core.wardrive_planner import channel_yield

# --- Real-shape fixtures -------------------------------------------------------------------------

_HDR_14 = ("MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,CurrentLatitude,CurrentLongitude,"
           "AltitudeMeters,AccuracyMeters,RCOIs,MfgrId,Type")
_HDR_11 = ("MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,"
           "AltitudeMeters,AccuracyMeters,Type")

# WigleWifi-1.4 (11 cols): NO Frequency, so RSSI@5, lat@6, lon@7.
CSV_14 = ("WigleWifi-1.4,appRelease=1,model=x,release=x,device=x,display=x,board=x,brand=x\n"
          + _HDR_11 + "\n"
          "AA:BB:CC:DD:EE:01,HomeNet,[WPA2-PSK-CCMP][ESS],2020-01-01 00:00:00,6,-42,47.6,-122.3,10,5,WIFI\n"
          "AA:BB:CC:DD:EE:02,OpenCafe,[ESS],2020-01-01 00:00:01,36,-55,47.61,-122.31,10,5,WIFI\n")

# WigleWifi-1.6 (14 cols): Frequency@5 pushes RSSI@6, lat@7, lon@8.
CSV_16 = ("WigleWifi-1.6,appRelease=1,model=x\n"
          + _HDR_14 + "\n"
          "AA:BB:CC:DD:EE:01,HomeNet,[WPA2-PSK-CCMP][ESS],2020-01-01 00:00:00,6,2437,-42,47.6,-122.3,10,5,,,WIFI\n"
          "AA:BB:CC:DD:EE:02,OpenCafe,[ESS],2020-01-01 00:00:01,36,5180,-55,47.61,-122.31,10,5,,,WIFI\n")

# Kismet's kismetdb_to_wiglecsv (older builds emit 1.4; placeholder device fields; AccuracyMeters=0).
CSV_KISMET_14 = ("WigleWifi-1.4,appRelease=Kismet2020.04.R1,model=Kismet,release=kismet,device=kismet,"
                 "display=kismet,board=kismet,brand=Kismet\n"
                 + _HDR_11 + "\n"
                 "AA:BB:CC:DD:EE:03,KismetNet,[WPA2-PSK-CCMP][ESS],2020-01-01 00:00:02,11,-60,51.5,-0.12,0,0,WIFI\n")


def test_reader_maps_1_4_by_name_lat_lon_at_index_6_7():
    rows = list(iter_wigle_rows(CSV_14))
    assert len(rows) == 2
    r = rows[0]
    assert (r["mac"], r["ssid"], r["channel"], r["rssi"], r["lat"], r["lon"]) == \
           ("AA:BB:CC:DD:EE:01", "HomeNet", "6", "-42", "47.6", "-122.3")


def test_reader_maps_1_6_by_name_lat_lon_at_index_7_8():
    rows = list(iter_wigle_rows(CSV_16))
    r = rows[0]
    # Same logical values as the 1.4 file even though the columns sit one place right (Frequency inserted).
    assert (r["channel"], r["rssi"], r["lat"], r["lon"]) == ("6", "-42", "47.6", "-122.3")


def test_reader_falls_back_to_1_6_positions_when_no_header():
    # A raw 1.6-shaped data row with no column-header line must read at the fixed 1.6 positions (the
    # behaviour every caller assumed before the fix — this is what keeps the old fixtures valid).
    raw = "AA:BB:CC:DD:EE:09,Raw,[OPEN],2020,11,2412,-70,12.3,45.6,0,0,,,WIFI\n"
    r = list(iter_wigle_rows(raw))[0]
    assert (r["lat"], r["lon"], r["rssi"], r["channel"]) == ("12.3", "45.6", "-70", "11")


def test_reader_strips_bom_and_reads_reordered_columns():
    # A UTF-8 BOM on line 1 + a producer that reorders columns: name-mapping resolves each field wherever
    # it sits, and the BOM doesn't break the first token.
    csv = (
        "﻿"                                   # a UTF-8 BOM, as its own token (see comment above)
        "WigleWifi-1.4,appRelease=1\n"
        # MAC stays field 0 (every real WiGLE file has it there — it's the data-row gate); the OTHER
        # columns are reordered (lat/lon before Channel/RSSI) to prove name-mapping, not position, wins.
        "MAC,SSID,CurrentLatitude,CurrentLongitude,Channel,RSSI,AuthMode,FirstSeen\n"
        "AA:BB:CC:DD:EE:04,MyNet,10.0,20.0,6,-33,[OPEN],2020\n"
    )
    r = list(iter_wigle_rows(csv))[0]
    assert (r["mac"], r["ssid"], r["lat"], r["lon"], r["rssi"], r["channel"]) == \
           ("AA:BB:CC:DD:EE:04", "MyNet", "10.0", "20.0", "-33", "6")


# --- The fix, proven through the three public readers -------------------------------------------

def test_summarize_reads_a_1_4_file_that_was_previously_dropped():
    s = summarize_wigle_csv(CSV_14)
    assert s["networks"] == 2                       # was 0 before the fix (every 11-col row dropped)
    assert s["wpa"] == 1 and s["open"] == 1
    assert s["band_24ghz"] == 1 and s["band_5ghz"] == 1   # ch 6 vs ch 36
    assert s["with_gps"] == 2
    assert s["rssi_strongest"] == -42 and s["rssi_weakest"] == -55


def test_points_reads_a_1_4_file_that_was_previously_dropped():
    pts = wigle_csv_to_points(CSV_14)
    assert len(pts) == 2
    by_mac = {bssid: (lat, lon, ssid) for (lat, lon, ssid, bssid) in pts}
    assert by_mac["AA:BB:CC:DD:EE:01"] == (47.6, -122.3, "HomeNet")   # lat/lon lifted from 1.4 index 6/7


def test_channel_yield_reads_a_1_4_file_that_was_previously_dropped():
    assert channel_yield(CSV_14) == {6: 1, 36: 1}   # was {} before the fix


def test_1_4_and_1_6_of_the_same_data_summarize_identically():
    # The whole point: version must not change the answer. 1.4 and 1.6 of the same two networks agree.
    assert summarize_wigle_csv(CSV_14) == summarize_wigle_csv(CSV_16)
    assert sorted(wigle_csv_to_points(CSV_14)) == sorted(wigle_csv_to_points(CSV_16))
    assert channel_yield(CSV_14) == channel_yield(CSV_16)


def test_kismet_1_4_wiglecsv_parses():
    s = summarize_wigle_csv(CSV_KISMET_14)
    assert s["networks"] == 1 and s["wpa"] == 1
    pts = wigle_csv_to_points(CSV_KISMET_14)
    assert pts == [(51.5, -0.12, "KismetNet", "AA:BB:CC:DD:EE:03")]


def test_concatenated_mixed_version_switches_mapping_per_header():
    # Two captures pasted into one file (a 1.4 block then a 1.6 block): each header re-binds the layout,
    # so rows after the 1.6 header read at 1.6 positions and rows after the 1.4 header at 1.4 positions.
    combined = CSV_14 + CSV_16
    pts = {bssid: (lat, lon) for (lat, lon, _s, bssid) in wigle_csv_to_points(combined)}
    assert pts["AA:BB:CC:DD:EE:01"] == (47.6, -122.3)   # correct from BOTH blocks (dedup collapses them)


def test_empty_and_header_only_never_crash():
    assert summarize_wigle_csv("")["networks"] == 0
    assert wigle_csv_to_points("WigleWifi-1.4\n" + _HDR_11 + "\n") == []
    assert channel_yield("") == {}


# --- Kismet .netxml importer (returns the same 4-tuple as wigle_csv_to_points) -------------------

# A real-shape Kismet netxml (DOCTYPE + <gps-info> avg/peak/min), grounded on kis2kml's fixture +
# WiGLE/Kismet docs (research wf_76746ecd). Net 2 has a real avg fix; net 3's avg is Kismet's 0/0
# no-fix sentinel and must fall back to peak; net 4 has only 0/0 (drop); net 5 has no gps-info (drop).
NETXML = (
    '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
    '<!DOCTYPE detection-run SYSTEM "http://kismetwireless.net/kismet-3.1.0.dtd">\n'
    '<detection-run kismet-version="2016.01.R1" start-time="Mon Dec 12 13:14:43 2016">\n'
    '  <wireless-network number="2" type="infrastructure">\n'
    "    <SSID><encryption>WPA2</encryption><essid cloaked=\"false\">Network 1</essid></SSID>\n"
    "    <BSSID>00:11:22:33:44:01</BSSID><channel>6</channel>\n"
    "    <gps-info><min-lat>-31.99</min-lat><min-lon>115.82</min-lon>"
    "<peak-lat>-31.985</peak-lat><peak-lon>115.824</peak-lon>"
    "<avg-lat>-31.9857</avg-lat><avg-lon>115.8240</avg-lon></gps-info>\n"
    "  </wireless-network>\n"
    '  <wireless-network number="3" type="infrastructure">\n'
    '    <SSID><essid cloaked="true"></essid></SSID><BSSID>00:11:22:33:44:02</BSSID>\n'
    "    <gps-info><avg-lat>0.000000</avg-lat><avg-lon>0.000000</avg-lon>"
    "<peak-lat>40.5</peak-lat><peak-lon>-74.1</peak-lon></gps-info>\n"
    "  </wireless-network>\n"
    '  <wireless-network number="4" type="probe">\n'
    '    <SSID><essid cloaked="false">NoFix</essid></SSID><BSSID>00:11:22:33:44:03</BSSID>\n'
    "    <gps-info><avg-lat>0.0</avg-lat><avg-lon>0.0</avg-lon></gps-info>\n"
    "  </wireless-network>\n"
    '  <wireless-network number="5" type="infrastructure">\n'
    '    <SSID><essid cloaked="false">NoGps</essid></SSID><BSSID>00:11:22:33:44:04</BSSID>\n'
    "  </wireless-network>\n"
    "</detection-run>\n"
)


def test_netxml_plots_positioned_networks_with_the_same_tuple_shape():
    pts = netxml_to_points(NETXML)
    by = {b: (lat, lon, s) for (lat, lon, s, b) in pts}
    assert by["00:11:22:33:44:01"] == (-31.9857, 115.8240, "Network 1")   # reads the avg- fix + essid


def test_netxml_falls_back_past_the_0_0_no_fix_sentinel_and_keeps_hidden_ssid():
    by = {b: (lat, lon, s) for (lat, lon, s, b) in netxml_to_points(NETXML)}
    # net 3: avg is Kismet's 0/0 sentinel, so the position comes from peak-; cloaked essid -> "".
    assert by["00:11:22:33:44:02"] == (40.5, -74.1, "")


def test_netxml_drops_unpositioned_networks():
    macs = {b for (_la, _lo, _s, b) in netxml_to_points(NETXML)}
    assert "00:11:22:33:44:03" not in macs   # only a 0/0 aggregate -> no real fix
    assert "00:11:22:33:44:04" not in macs   # no <gps-info> at all


def test_netxml_tolerates_garbage_and_empty():
    assert netxml_to_points("not xml") == []
    assert netxml_to_points("") == []
    assert netxml_to_points("<detection-run></detection-run>") == []


def test_netxml_refuses_xml_entities_defusedxml():
    # An entity-bearing file must import as no points, never expand (billion-laughs / XXE defence).
    bomb = ('<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "x">]>'
            "<detection-run><wireless-network><BSSID>00:11:22:33:44:09</BSSID>"
            "<gps-info><avg-lat>1.0</avg-lat><avg-lon>2.0</avg-lon></gps-info>"
            "</wireless-network></detection-run>")
    assert netxml_to_points(bomb) == []
