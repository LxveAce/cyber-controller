"""Marauder scan-index ordinal (comms rework, 1.5). The scanall stream prints no per-AP index, so the parser
assigns one by discovery order (deduped by BSSID) — that's what `select -a {index}` / target_ingest's
extra['index'] bind to. Final list-vs-scan boundary behavior is bench-gated; the ordinal logic lands here.
"""
from __future__ import annotations

from src.protocols.marauder import MarauderProtocol

# Multi-line scanall form (v1.12.3): ESSID -> BSSID -> RSSI per AP.
SCAN_DUMP = [
    "ESSID: NetA",
    "BSSID: aa:bb:cc:dd:ee:01",
    "RSSI: -40",
    "ESSID: NetB",
    "BSSID: aa:bb:cc:dd:ee:02",
    "RSSI: -55",
    "ESSID: NetC",
    "BSSID: aa:bb:cc:dd:ee:03",
    "RSSI: -60",
]


def _ap_events(proto, lines):
    out = []
    for ln in lines:
        ev = proto.parse_line(ln)
        if ev is not None and ev.event_type == "ap_found":
            out.append(ev)
    return out


def test_ap_found_carries_running_index():
    aps = _ap_events(MarauderProtocol(), SCAN_DUMP)
    assert [e.data["index"] for e in aps] == [0, 1, 2]
    assert [e.data["ssid"] for e in aps] == ["NetA", "NetB", "NetC"]


def test_reseen_bssid_keeps_its_index():
    p = MarauderProtocol()
    _ap_events(p, SCAN_DUMP)  # 0,1,2
    # NetB re-observed in a later scan pass keeps index 1 (stable list position), doesn't get a new ordinal.
    again = _ap_events(p, ["ESSID: NetB", "BSSID: aa:bb:cc:dd:ee:02", "RSSI: -50"])
    assert len(again) == 1 and again[0].data["index"] == 1


def test_reset_scan_index_restarts_from_zero():
    p = MarauderProtocol()
    _ap_events(p, SCAN_DUMP)
    p.reset_scan_index()
    aps = _ap_events(p, ["ESSID: FreshNet", "BSSID: aa:bb:cc:dd:ee:09", "RSSI: -33"])
    assert aps[0].data["index"] == 0


def test_single_line_form_also_indexes():
    p = MarauderProtocol()
    e0 = p.parse_line("SSID: One BSSID: aa:bb:cc:dd:ee:aa Ch: 1 RSSI: -30")
    e1 = p.parse_line("SSID: Two BSSID: aa:bb:cc:dd:ee:bb Ch: 6 RSSI: -44")
    assert e0.data["index"] == 0 and e1.data["index"] == 1
