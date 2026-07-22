"""Tests for the Flock-You ALPR-detector protocol parser + ALPR target ingest (FL F1).

The firmware's serial schema is grounded in upstream main.cpp (promiscious-dev, 2026-07-03) but can
drift, so these tests pin the tolerant-parsing contract: recognise the JSON line and the human mirror
line, never raise on malformed input, and route detections to TargetType.ALPR.
"""
from __future__ import annotations

import pytest

from src.core.target_ingest import TargetIngestor
from src.models.target import Target, TargetType
from src.protocols import (
    PROTOCOLS,
    PROTOCOL_DISPLAY_NAMES,
    get_protocol,
    get_protocol_module,
    list_protocols,
)
from src.protocols.base import ParsedEvent
from src.protocols.flock_you import FlockYouProtocol

# Real emit shapes (from upstream main.cpp emitDetectionJSON + dualPrintf DETECT lines).
JSON_LINE = (
    '{"event":"detection","detection_method":"wifi_wildcard_probe_ie_sig","protocol":"wifi_2_4ghz",'
    '"mac_address":"AA:BB:CC:11:22:33","oui":"AABBCC","device_name":"","rssi":-61,"channel":6,'
    '"frequency":2437,"ssid":"FlockCam"}'
)
HUMAN_SSID = '[flockyou] DETECT-SSID type=probe mac=AA:BB:CC:11:22:33 ssid="FlockCam" rssi=-55 ch=6 count=3'
HUMAN_OUI = "[flockyou] DETECT-OUI mac=DE:AD:BE:EF:00:11 oui=DEADBE rssi=-70 ch=1 addr=addr2 count=1"


@pytest.fixture
def proto() -> FlockYouProtocol:
    return FlockYouProtocol()


# ── Registration / wiring ────────────────────────────────────────────
def test_registered_under_flock_you():
    assert "flock-you" in PROTOCOLS
    assert PROTOCOLS["flock-you"] is FlockYouProtocol
    assert "flock-you" in PROTOCOL_DISPLAY_NAMES


def test_get_protocol_resolves_both_spellings():
    # The profile binds protocol="flock_you"; get_protocol must normalise the underscore.
    assert isinstance(get_protocol("flock_you"), FlockYouProtocol)
    assert isinstance(get_protocol("flock-you"), FlockYouProtocol)


def test_passive_sensor_is_not_text_cli():
    # QA-6 #3: Flock-You is a passive receive-only sensor with no command channel, so it must NOT be
    # a "text-cli" node — else the connect probe writes an unsolicited `help` it can never answer.
    proto = FlockYouProtocol()
    assert proto.driver_type != "text-cli"       # non-CLI -> probe reports "no-cli", writes nothing
    assert proto.get_commands() == []            # and it genuinely has nothing to send


def test_profile_binds_the_parser():
    import json
    from pathlib import Path

    cfg = json.loads(
        (Path(__file__).resolve().parents[1] / "src/config/profiles/flock_you.json").read_text("utf-8")
    )
    assert cfg["protocol"] == "flock_you"
    assert isinstance(get_protocol(cfg["protocol"]), FlockYouProtocol)


# ── JSON detection line ──────────────────────────────────────────────
def test_json_detection_parses_to_alpr_found(proto):
    ev = proto.parse_line(JSON_LINE)
    assert ev is not None and ev.event_type == "alpr_found"
    assert ev.data["mac"] == "AA:BB:CC:11:22:33"
    assert ev.data["ssid"] == "FlockCam"
    assert ev.data["rssi"] == -61
    assert ev.data["channel"] == 6
    assert ev.data["oui"] == "AABBCC"
    assert ev.data["detection_method"] == "wifi_wildcard_probe_ie_sig"
    assert ev.data["frequency"] == 2437


def test_json_with_leading_whitespace(proto):
    ev = proto.parse_line("   " + JSON_LINE + "  ")
    assert ev is not None and ev.event_type == "alpr_found"
    assert ev.data["mac"] == "AA:BB:CC:11:22:33"


def test_json_missing_optional_fields_degrades_not_raises(proto):
    ev = proto.parse_line('{"event":"detection","mac_address":"11:22:33:44:55:66"}')
    assert ev is not None and ev.event_type == "alpr_found"
    assert ev.data["mac"] == "11:22:33:44:55:66"
    assert ev.data["rssi"] == 0 and ev.data["channel"] == 0  # defaulted, no crash


# ── Human mirror lines ───────────────────────────────────────────────
def test_human_ssid_line(proto):
    ev = proto.parse_line(HUMAN_SSID)
    assert ev is not None and ev.event_type == "alpr_found"
    assert ev.data["mac"] == "AA:BB:CC:11:22:33"
    assert ev.data["ssid"] == "FlockCam"
    assert ev.data["rssi"] == -55
    assert ev.data["channel"] == 6


def test_human_oui_line(proto):
    ev = proto.parse_line(HUMAN_OUI)
    assert ev is not None and ev.event_type == "alpr_found"
    assert ev.data["mac"] == "DE:AD:BE:EF:00:11"
    assert ev.data["oui"] == "DEADBE"
    assert ev.data["rssi"] == -70
    assert ev.data["channel"] == 1


# ── Tolerance: malformed / status / noise never raise ────────────────
@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "random serial noise not from flockyou",
        '{"event":"detection","mac_address":',  # truncated JSON
        "{not even json",
        "[flockyou]",  # bare tag
        '{"event":"other","foo":1}',  # JSON, but not a detection
    ],
)
def test_tolerant_never_raises(proto, line):
    # The contract: parse_line must never throw on any input line.
    proto.parse_line(line)  # must not raise


def test_status_line_is_info_not_target(proto):
    ev = proto.parse_line("[flockyou] booting, SPIFFS ready, hopping 11/6/1")
    assert ev is not None and ev.event_type == "info"
    assert "SPIFFS" in ev.data["message"]


def test_truncated_json_does_not_become_a_target(proto):
    ev = proto.parse_line('{"event":"detection","mac_address":')
    # Malformed JSON with no human/status shape -> noise, not a phantom ALPR target.
    assert ev is None


def test_non_detection_json_ignored(proto):
    assert proto.parse_line('{"event":"heartbeat","uptime":1234}') is None


# ── identify() / commands ────────────────────────────────────────────
def test_identify(proto):
    assert proto.identify(HUMAN_OUI) is True
    assert proto.identify(JSON_LINE) is True
    assert proto.identify("SSID: HomeWiFi | BSSID: ...") is False


def test_passive_no_commands(proto):
    assert proto.get_commands() == []
    assert proto.capabilities == frozenset({"wifi", "gps"})


# ── Ingest: alpr_found -> TargetType.ALPR ────────────────────────────
def test_ingest_alpr_event_to_target():
    ev = ParsedEvent(
        event_type="alpr_found",
        data={"mac": "AA:BB:CC:11:22:33", "ssid": "FlockCam", "rssi": -61, "channel": 6,
              "oui": "AABBCC", "detection_method": "wifi_wildcard_probe_ie_sig", "frequency": 2437},
        raw=JSON_LINE,
    )
    t = TargetIngestor._event_to_target(ev, "COM7")
    assert t is not None
    assert t.target_type is TargetType.ALPR
    assert t.mac == "AA:BB:CC:11:22:33"
    assert t.ssid == "FlockCam"
    assert t.channel == 6
    assert t.device_source == "COM7"
    assert t.extra["oui"] == "AABBCC"
    assert t.extra["detection_method"] == "wifi_wildcard_probe_ie_sig"
    assert t.key == "alpr:AA:BB:CC:11:22:33"  # dedup key namespaced by type


def test_ingest_alpr_without_ssid_falls_back_to_method():
    ev = ParsedEvent(
        event_type="alpr_found",
        data={"mac": "DE:AD:BE:EF:00:11", "ssid": "", "detection_method": "wifi_oui_addr2"},
        raw="",
    )
    t = TargetIngestor._event_to_target(ev, "COM3")
    assert t is not None and t.ssid == "wifi_oui_addr2"


def test_ingest_alpr_empty_mac_dropped():
    ev = ParsedEvent(event_type="alpr_found", data={"mac": "", "ssid": "x"}, raw="")
    assert TargetIngestor._event_to_target(ev, "COM3") is None


def test_end_to_end_line_to_target():
    """A raw serial line runs through the parser, then the ingestor, into an ALPR target."""
    proto = FlockYouProtocol()
    ev = proto.parse_line(JSON_LINE)
    t = TargetIngestor._event_to_target(ev, "COM9")
    assert t is not None and t.target_type is TargetType.ALPR and t.mac == "AA:BB:CC:11:22:33"


# ── Graph kind (surveillance cameras are not APs) ────────────────────
def test_network_tab_kind_for_alpr():
    pytest.importorskip("PyQt5")
    from src.models.target import Target
    from src.ui.qt.network_tab import _KIND_COLORS, NetworkTab

    assert "alpr" in _KIND_COLORS
    t = Target(mac="AA:BB:CC:11:22:33", target_type=TargetType.ALPR)
    assert NetworkTab._target_kind(t) == "alpr"


# ── Safety invariant: an ALPR camera is offered ZERO attack actions ──
def test_no_protocol_arms_alpr():
    """Regression guard for the awareness-only invariant: no firmware's TARGET_ACTIONS table may
    declare actions for TargetType.ALPR. If a future edit adds one, ALPR silently becomes an armed
    target in the action menus — this test fails first."""
    offenders = []
    for name in list_protocols():
        mod = get_protocol_module(name)
        actions = getattr(mod, "TARGET_ACTIONS", None) if mod else None
        if isinstance(actions, dict) and TargetType.ALPR in actions:
            offenders.append(name)
    assert not offenders, f"TargetType.ALPR must have no attack actions, but these declare them: {offenders}"


# ── End-to-end (human OUI line) + serialization round-trip ───────────
def test_human_oui_end_to_end():
    ev = FlockYouProtocol().parse_line(HUMAN_OUI)
    t = TargetIngestor._event_to_target(ev, "COM4")
    assert t is not None and t.target_type is TargetType.ALPR
    assert t.mac == "DE:AD:BE:EF:00:11" and t.channel == 1
    assert t.extra.get("oui") == "DEADBE"


def test_alpr_target_roundtrips_through_dict():
    t = Target(mac="AA:BB:CC:11:22:33", target_type=TargetType.ALPR, ssid="FlockCam",
               extra={"oui": "AABBCC"})
    back = Target.from_dict(t.to_dict())
    assert back.target_type is TargetType.ALPR and back.mac == t.mac and back.ssid == "FlockCam"


def test_nested_mac_address_is_dropped_not_phantom():
    """A drifted line with a typed/nested mac_address must not manufacture a garbage-MAC target."""
    ev = FlockYouProtocol().parse_line('{"event":"detection","mac_address":{"x":1},"rssi":-61}')
    # Either the parser recovered no MAC (empty) so ingest drops it, or it returned noise — never a target.
    t = TargetIngestor._event_to_target(ev, "COM4") if ev is not None else None
    assert t is None


def test_float_rssi_truncates_not_zeroed():
    ev = FlockYouProtocol().parse_line(
        '{"event":"detection","mac_address":"AA:BB:CC:11:22:33","rssi":-61.5,"channel":6}'
    )
    assert ev is not None and ev.data["rssi"] == -61  # truncated, not defaulted to 0
