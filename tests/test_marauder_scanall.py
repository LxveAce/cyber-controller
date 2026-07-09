"""Marauder `scanall` single-line AP parsing feeds the shared TargetPool.

Real Marauder v1.12.3 `scanall` prints each AP as ONE line with a bare leading RSSI, a mid-line BSSID and
NO field labels: ``-52 Ch: 6 aa:bb:cc:dd:ee:ff ESSID: MyNet 11 15``. The protocol parser used to recognise
only the legacy ``SSID:..BSSID:..`` line and the anchored multi-line ESSID/BSSID/RSSI form, so every live
scan fell through to ``info`` and the shared TargetPool stayed empty — Targets, Macro-fill, Cross-Comm and
the network graph all showed nothing. These tests lock in the single-line parse (shared with the wardrive
extractor) and guard against Client/BLE/status lines misfiring as APs.
"""
from src.protocols.marauder import MarauderProtocol


def _p():
    return MarauderProtocol()


def test_scanall_single_line_ap_emits_ap_found():
    ev = _p().parse_line("-52 Ch: 6 aa:bb:cc:dd:ee:ff ESSID: MyNet 11 15")
    assert ev is not None and ev.event_type == "ap_found"
    assert ev.data["bssid"] == "aa:bb:cc:dd:ee:ff"
    assert ev.data["ssid"] == "MyNet"          # trailing "11 15" metadata columns stripped, not in SSID
    assert ev.data["rssi"] == -52
    assert ev.data["channel"] == 6


def test_scanall_hidden_ssid_ap_still_found():
    # A hidden AP prints no ESSID; the bare-leading-RSSI signature must still yield an ap_found.
    ev = _p().parse_line("-70 Ch: 1 11:22:33:44:55:66")
    assert ev is not None and ev.event_type == "ap_found"
    assert ev.data["bssid"] == "11:22:33:44:55:66"
    assert ev.data["ssid"] == ""


def test_scanall_ssid_with_interior_spaces_and_digits_preserved():
    ev = _p().parse_line("-61 Ch: 11 de:ad:be:ef:00:11 ESSID: DIRECT-50 HP Smart Tank 5100 11 05")
    assert ev.event_type == "ap_found"
    assert ev.data["ssid"] == "DIRECT-50 HP Smart Tank 5100"   # only the two trailing metadata cols removed


def test_client_ble_status_do_not_misfire_as_ap():
    p = _p()
    assert p.parse_line("Client: 12:34:56:78:9a:bc AP: aa:bb:cc:dd:ee:ff").event_type == "client_found"
    assert p.parse_line("BLE: 12:34:56:78:9a:bc Name: Fitbit RSSI: -40").event_type == "ble_found"
    assert p.parse_line("> scanning...").event_type == "status"


def test_multiline_ap_form_still_works():
    p = _p()
    p.parse_line("ESSID: MultiNet")
    p.parse_line("BSSID: 99:88:77:66:55:44")
    ev = p.parse_line("RSSI: -33")
    assert ev.event_type == "ap_found"
    assert ev.data["ssid"] == "MultiNet" and ev.data["bssid"] == "99:88:77:66:55:44"


def test_scanall_aps_get_stable_incrementing_indices():
    p = _p()
    a = p.parse_line("-40 Ch: 1 aa:aa:aa:aa:aa:aa ESSID: A 11 01")
    b = p.parse_line("-50 Ch: 6 bb:bb:bb:bb:bb:bb ESSID: B 11 06")
    a2 = p.parse_line("-42 Ch: 1 aa:aa:aa:aa:aa:aa ESSID: A 11 01")  # re-seen keeps its index
    assert a.data["index"] == 0
    assert b.data["index"] == 1
    assert a2.data["index"] == 0
