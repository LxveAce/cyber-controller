"""ESP32-DIV (LxveLabs serial fork) protocol — pure parser tests against the LXVEDIV/1 wire spec
(command-center/projects/cc-app/LXVEDIV-SERIAL-PROTOCOL-2026-07-21.md). No hardware; canned lines only.
"""
from __future__ import annotations

import pytest

pytest.importorskip("src.protocols")

from src.core import safety
from src.protocols import PROTOCOL_DISPLAY_NAMES, get_protocol


def _p():
    return get_protocol("esp32-div-serial")


def test_registered_and_functional_but_not_yet_advertised():
    # The parser is registered + works (identify/get_protocol), so a fork board auto-routes to it...
    assert _p().protocol_name == "esp32-div-serial"
    # ...but it has NO public display name yet (the fork firmware doesn't exist, so it must not inflate the
    # advertised parser count). The display name + "(stock)" rename land when the fork ships.
    assert "esp32-div-serial" not in PROTOCOL_DISPLAY_NAMES
    assert PROTOCOL_DISPLAY_NAMES["esp32-div"] == "ESP32-DIV"


def test_identify_only_the_lxvediv_banner():
    p = _p()
    assert p.identify("LXVEDIV/1 fork=1.0.0 base=v3.2 board=cyd caps=0x7 heap=180000")
    assert p.identify("lxvediv/2 fork=1.1.0")           # case-insensitive, version-tolerant
    assert not p.identify("ESP32-DIV v3.2")             # a stock banner is NOT our fork
    assert not p.identify("AP idx=0 ssid=Home bssid=aa:bb:cc:dd:ee:ff ch=6 rssi=-40 enc=WPA2")


def test_identity_line_is_a_status_event():
    ev = _p().parse_line("LXVEDIV/1 fork=1.0.0 base=v3.2 caps=0x7")
    assert ev is not None and ev.event_type == "status"


def test_ap_line_populates_ap_found_with_matching_keys():
    ev = _p().parse_line("AP idx=3 ssid=CoffeeShop bssid=AA:BB:CC:DD:EE:FF ch=11 rssi=-52 enc=WPA2")
    assert ev.event_type == "ap_found"
    assert ev.data["ssid"] == "CoffeeShop"
    assert ev.data["bssid"] == "AA:BB:CC:DD:EE:FF"
    assert ev.data["channel"] == 11 and ev.data["rssi"] == -52
    assert ev.data["encryption"] == "WPA2" and ev.data["index"] == 3


def test_station_line_is_client_found():
    ev = _p().parse_line("STA idx=1 mac=11:22:33:44:55:66 ap=AA:BB:CC:DD:EE:FF rssi=-60")
    assert ev.event_type == "client_found"
    assert ev.data["mac"] == "11:22:33:44:55:66"
    assert ev.data["bssid"] == "AA:BB:CC:DD:EE:FF" and ev.data["rssi"] == -60


def test_ble_line_is_ble_found():
    ev = _p().parse_line("BLE idx=0 mac=DE:AD:BE:EF:00:11 name=AirPods rssi=-44")
    assert ev.event_type == "ble_found"
    assert ev.data["mac"] == "DE:AD:BE:EF:00:11" and ev.data["name"] == "AirPods"


def test_capture_lines():
    e1 = _p().parse_line("PMKID bssid=AA:BB:CC:DD:EE:FF pmkid=deadbeefcafe")
    assert e1.event_type == "pmkid_captured" and e1.data["pmkid"] == "deadbeefcafe"
    e2 = _p().parse_line("EAPOL bssid=AA:BB:CC:DD:EE:FF sta=11:22:33:44:55:66 msg=2")
    assert e2.event_type == "handshake_captured" and e2.data["bssid"] == "AA:BB:CC:DD:EE:FF"


def test_attack_deauth_start_registers_deauth():
    ev = _p().parse_line("ATTACK verb=deauth state=start target=AA:BB:CC:DD:EE:FF")
    assert ev.event_type == "deauth_sent" and ev.data["target"] == "AA:BB:CC:DD:EE:FF"
    # a stopped attack (or spam/jam) is informational, not a deauth event
    assert _p().parse_line("ATTACK verb=deauth state=stop target=all").event_type == "info"


def test_unknown_line_is_info_passthrough():
    assert _p().parse_line("JAM state=stub reason=owner-completes").event_type == "info"
    assert _p().parse_line("some random debug text").event_type == "info"
    assert _p().parse_line("") is None


def test_command_catalog_danger_levels():
    cmds = {ci.name: ci for ci in _p().cached_commands()}
    assert safety.classify(cmds["deauth"].name, cmds["deauth"]) == safety.LAB_ONLY
    assert safety.classify(cmds["blespam apple"].name, cmds["blespam apple"]) == safety.LAB_ONLY
    assert safety.classify(cmds["nrf jam"].name, cmds["nrf jam"]) == safety.ILLEGAL_TX
    for safe_verb in ("scanwifi", "scanall", "list ap", "status", "sniff start"):
        assert safety.classify(cmds[safe_verb].name, cmds[safe_verb]) == safety.SAFE, safe_verb
