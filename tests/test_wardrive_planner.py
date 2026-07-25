"""Unit tests for the wardrive channel-coverage planner (pure, no hardware, no owner data).

The fixture is a small SYNTHETIC WiGLE-1.6 CSV with a known channel distribution — a public documented
format, not owner capture data — so the algorithm's mechanics are asserted exactly. It deliberately
includes a duplicate BSSID (append-only re-sighting), a channel-0 row, and a garbled row to exercise the
dedup + tolerance, and 5 GHz channel 36 to prove the plan is derived from the data (no {1, 6, 11} prior).
"""
from src.core.wardrive import WIGLE_HEADER
from src.core.wardrive_planner import (
    assign_nodes,
    channel_yield,
    cumulative_coverage,
    format_plan,
)

# 14-column WiGLE rows: MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,Lat,Lon,Alt,Acc,RCOIs,Mfgr,Type
_ROWS = [
    "AA:AA:AA:AA:AA:01,net1,[WPA2],2024-01-01,6,2437,-40,0,0,0,0,,,WIFI",
    "AA:AA:AA:AA:AA:01,net1,[WPA2],2024-01-01,6,2437,-35,0,0,0,0,,,WIFI",  # dup BSSID, stronger -> still 1
    "BB:BB:BB:BB:BB:02,net2,[WPA2],2024-01-01,6,2437,-50,0,0,0,0,,,WIFI",
    "CC:CC:CC:CC:CC:03,net3,[OPEN],2024-01-01,6,2437,-60,0,0,0,0,,,WIFI",
    "DD:DD:DD:DD:DD:04,net4,[WPA2],2024-01-01,1,2412,-45,0,0,0,0,,,WIFI",
    "EE:EE:EE:EE:EE:05,net5,[WPA2],2024-01-01,1,2412,-55,0,0,0,0,,,WIFI",
    "FF:FF:FF:FF:FF:06,net6,[WPA2],2024-01-01,11,2462,-48,0,0,0,0,,,WIFI",
    "11:11:11:11:11:07,net7,[WPA2],2024-01-01,36,5180,-52,0,0,0,0,,,WIFI",  # 5 GHz, data-derived
    "22:22:22:22:22:08,net8,[WPA2],2024-01-01,36,5180,-58,0,0,0,0,,,WIFI",
    "33:33:33:33:33:09,net9,[WPA2],2024-01-01,0,0,-70,0,0,0,0,,,WIFI",  # channel 0 -> excluded
    "short,row",  # garbled -> skipped
]
# WigleWifi pre-header + the column header + the data rows — exactly what WardriveSession writes.
FIXTURE = "WigleWifi-1.6,appRelease=cc\n" + WIGLE_HEADER + "\n" + "\n".join(_ROWS) + "\n"


def test_channel_yield_dedups_by_bssid_and_drops_channel_zero():
    y = channel_yield(FIXTURE)
    assert y == {6: 3, 1: 2, 11: 1, 36: 2}  # dup AA counted once; ch0 + garbled excluded


def test_channel_yield_empty_and_headers_only():
    assert channel_yield("") == {}
    assert channel_yield("WigleWifi-1.6\n" + WIGLE_HEADER + "\n") == {}


def test_cumulative_coverage_curve_and_tie_break():
    curve = cumulative_coverage(channel_yield(FIXTURE))
    # ranked busiest-first; ch1 and ch36 tie at 2 -> ascending channel wins (ch1 before ch36).
    assert [c[0] for c in curve] == [6, 1, 36, 11]
    assert [c[1] for c in curve] == [3, 5, 7, 8]  # cumulative distinct networks
    assert curve[0][2] == 37.5 and curve[-1][2] == 100.0  # 3/8 and 8/8


def test_assign_nodes_picks_top_yield_channels():
    y = channel_yield(FIXTURE)
    assert assign_nodes(y, 2) == [6, 1]  # busiest two (tie -> ch1 over ch36)
    assert assign_nodes(y, 1) == [6]
    assert assign_nodes(y, 0) == []
    assert assign_nodes(y, 99) == [6, 1, 36, 11]  # more nodes than channels -> all channels


def test_assign_nodes_never_assumes_1_6_11():
    # A capture with NO 2.4 GHz control channels must still plan from its own data.
    only_5ghz = "WigleWifi-1.6\n" + WIGLE_HEADER + "\n" + "\n".join([
        "AA:AA:AA:AA:AA:01,a,[WPA2],2024-01-01,149,5745,-40,0,0,0,0,,,WIFI",
        "BB:BB:BB:BB:BB:02,b,[WPA2],2024-01-01,149,5745,-50,0,0,0,0,,,WIFI",
        "CC:CC:CC:CC:CC:03,c,[WPA2],2024-01-01,44,5220,-55,0,0,0,0,,,WIFI",
    ]) + "\n"
    assert channel_yield(only_5ghz) == {149: 2, 44: 1}
    assert assign_nodes(channel_yield(only_5ghz), 1) == [149]


def test_format_plan_is_ascii_and_reports_coverage():
    out = format_plan(FIXTURE, nodes=2)
    assert out.isascii()  # console-safe
    assert "8 distinct network(s) across 4 channel(s)" in out
    assert "ch6" in out and "channels [6, 1]" in out
    assert "covers 5/8 = 62.5%" in out  # top-2 channels cover 5 of 8 networks


def test_format_plan_empty_input():
    out = format_plan("", nodes=3)
    assert "0 distinct network(s)" in out
    assert "no channelled networks found" in out
