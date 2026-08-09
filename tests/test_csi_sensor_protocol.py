"""WS1 Wi-Fi CSI sensing node protocol — pure parser tests against the node firmware verdict format
(``firmware/node/node.ino`` emits ``csi presence=<0|1> motion=<0..1> conf=<0..1>``). No hardware;
canned verdict lines only, grounded in the firmware's own snprintf format via src.core.sensing.
"""
from __future__ import annotations

import pytest

pytest.importorskip("src.protocols")

from src.core import sensing
from src.protocols import PROTOCOL_DISPLAY_NAMES, get_protocol


def _p():
    return get_protocol("csi-sensor")


def test_registered_and_functional_but_not_yet_advertised():
    # The parser is registered + works, so a sensing node auto-routes to it...
    assert _p().protocol_name == "csi-sensor"
    # ...but it has NO public display name yet (node firmware is compile-only, not HW-validated, so
    # it must not inflate the advertised parser count). Same posture as esp32-div-serial.
    assert "csi-sensor" not in PROTOCOL_DISPLAY_NAMES


def test_is_a_passive_receive_only_sensor():
    p = _p()
    assert p.get_commands() == []          # nothing to send — a sensing node is read, not driven
    assert p.driver_type == "controlmap"   # no text CLI (mirrors DroneMesh/FlockYou)
    assert "wifi" in p.capabilities


def test_verdict_line_becomes_a_sensing_verdict_event():
    ev = _p().parse_line("csi presence=1 motion=0.42 conf=0.82")
    assert ev is not None and ev.event_type == "sensing_verdict"
    assert ev.data["presence"] is True
    assert abs(ev.data["motion"] - 0.42) < 1e-6
    assert abs(ev.data["confidence"] - 0.82) < 1e-6
    assert ev.data["tier"] == sensing.PROVEN


def test_idle_verdict_parses_to_empty_room():
    ev = _p().parse_line("csi presence=0 motion=0.00 conf=0.00")
    assert ev.event_type == "sensing_verdict"
    assert ev.data["presence"] is False
    assert ev.data["motion"] == 0.0 and ev.data["confidence"] == 0.0


def test_identify_only_a_real_verdict_line():
    p = _p()
    assert p.identify("csi presence=1 motion=0.30 conf=0.7")
    assert p.identify("csi presence=0")                       # tolerant: presence alone is enough
    # non-verdict serial noise must NOT be claimed as a sensing node
    assert not p.identify("ESP32-DIV v3.2")
    assert not p.identify("AP idx=0 ssid=Home ch=6 rssi=-40")
    assert not p.identify("csi motion=0.3")                    # no presence field -> not a verdict
    assert not p.identify("")


def test_non_verdict_lines_are_ignored():
    # Unlike the generic passthrough, a sensing parser stays quiet on noise (only verdicts matter).
    assert _p().parse_line("boot ok") is None
    assert _p().parse_line("heartbeat") is None
    assert _p().parse_line("") is None


def test_sensing_verdict_is_not_a_scan_target():
    # sensing_verdict is a NEW event type (like drone_found) — never a Target-pool row. Guard the
    # event type, so a future target_ingest change can't silently route a person as a "target".
    ev = _p().parse_line("csi presence=1 motion=0.5 conf=0.6")
    assert ev.event_type == "sensing_verdict"
    assert "mac" not in ev.data and "bssid" not in ev.data
