"""AutoRouter MAC-shape gate must not drop non-WiFi targets whose rule doesn't interpolate {mac}.

Non-WiFi targets carry a non-MAC identifier in ``mac`` (SubGHz frequency, NFC/RFID UID, a BW16 index
key). The old unconditional ``_MAC_RE`` gate rejected the WHOLE target with a "malformed MAC" warning
even when the rule's command_template never used {mac}, so every non-WiFi target was silently unroutable.
The gate now applies only when the template actually contains {mac}. Pure logic, no hardware."""

from __future__ import annotations

import pytest

cross_comm = pytest.importorskip("src.core.cross_comm")


def _rule(**kw):
    from src.core.cross_comm import RoutingRule
    base = dict(name="r", target_type=None, ssid_pattern="", min_rssi=-100,
                device_port="COMX", command_template="nfc emulate {ssid}", cooldown=0.0, enabled=True)
    base.update(kw)
    return RoutingRule(**base)


def _fire(rule, payload):
    bus = cross_comm.EventBus()
    sends: list[tuple[str, str]] = []
    router = cross_comm.AutoRouter(bus, lambda port, cmd: sends.append((port, cmd)))
    router.add_rule(rule)
    router._on_target("target.added", dict(payload))
    return sends


def test_non_mac_target_routes_when_template_has_no_mac():
    # NFC target: mac holds a 20-char UID (not MAC-shaped). A rule using only {ssid} must still fire.
    sends = _fire(_rule(command_template="nfc emulate {ssid}"),
                  {"target_type": "nfc", "mac": "04:A2:2B:3C:4D:5E:6F", "ssid": "MyTag", "rssi": 0, "channel": 0})
    assert sends == [("COMX", "nfc emulate MyTag")]


def test_subghz_frequency_target_routes_without_mac_template():
    # SubGHz target: mac holds a frequency string like "433.92" (contains '.', not MAC-shaped).
    sends = _fire(_rule(command_template="subghz tx --preset ook"),
                  {"target_type": "subghz", "mac": "433.92", "ssid": "", "rssi": 0, "channel": 0})
    assert sends == [("COMX", "subghz tx --preset ook")]


def test_mac_template_still_rejects_a_malformed_mac():
    # A rule that DOES interpolate {mac} keeps guarding the shape: a non-MAC value is rejected, not sent.
    sends = _fire(_rule(command_template="deauth {mac}"),
                  {"target_type": "subghz", "mac": "433.92", "ssid": "", "rssi": 0, "channel": 0})
    assert sends == []


def test_mac_template_sends_for_a_valid_mac():
    sends = _fire(_rule(command_template="deauth {mac}"),
                  {"target_type": "ap", "mac": "AA:BB:CC:DD:EE:FF", "ssid": "", "rssi": 0, "channel": 0})
    assert sends == [("COMX", "deauth AA:BB:CC:DD:EE:FF")]
