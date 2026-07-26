"""DroneMeshProtocol — the passive drone-mesh-mapper Remote-ID parser (RX-only, no command channel).

Feeds real firmware serial lines (the same golden JSON grounded in remoteid-mesh/src/main.cpp that
tests/test_remote_id.py uses) through parse_line and asserts a drone_found event with every field,
plus the degraded Meshtastic-relay path and the rejections. A GUARD test proves a drone_found
event is NOT routed into the shared Target pool (target_ingest None) — the slice stays off the
spine. Nothing here transmits.
"""
from __future__ import annotations

from src.core.cross_comm import EventBus, TargetPool
from src.core.target_ingest import TargetIngestor
from src.protocols.base import ParsedEvent
from src.protocols.drone_mesh import DroneMeshProtocol

# A complete USB-Serial detection line as the firmware emits it (both drone + pilot positions).
_FULL = (
    '{"mac":"aa:bb:cc:dd:ee:ff", "rssi":-63, "drone_lat":37.774929, "drone_long":-122.419418, '
    '"drone_altitude":152, "pilot_lat":37.775100, "pilot_long":-122.420000, '
    '"basic_id":"1581F5XYZ0000000ABCD"}'
)
_HEARTBEAT = '{"heartbeat":"Device is active and running."}'


def test_usb_json_detection_becomes_drone_found():
    ev = DroneMeshProtocol().parse_line(_FULL)
    assert ev is not None and ev.event_type == "drone_found"
    d = ev.data
    assert d["mac"] == "aa:bb:cc:dd:ee:ff"
    assert d["basic_id"] == "1581F5XYZ0000000ABCD"
    assert d["rssi"] == -63
    assert d["drone_lat"] == 37.774929 and d["drone_long"] == -122.419418
    assert d["drone_altitude"] == 152
    assert d["pilot_lat"] == 37.775100 and d["pilot_long"] == -122.420000
    assert d["has_drone_location"] is True and d["has_pilot_location"] is True
    assert d["label"] == "1581F5XYZ0000000ABCD" and d["source"] == "usb"


def test_heartbeat_is_info_not_a_detection():
    ev = DroneMeshProtocol().parse_line(_HEARTBEAT)
    assert ev is not None and ev.event_type == "info"


def test_non_detection_lines_are_rejected():
    p = DroneMeshProtocol()
    assert p.parse_line("") is None
    assert p.parse_line("random boot chatter") is None
    assert p.parse_line('{"rssi":-63}') is None          # JSON with no mac -> not a detection
    assert p.parse_line('{"mac":"aa:bb", "rssi":}') is None  # malformed JSON


def test_mesh_relay_drone_then_pilot_correlates_on_mac():
    p = DroneMeshProtocol()
    drone = p.parse_line(
        "Drone: aa:bb:cc:dd:ee:ff RSSI:-63 https://maps.google.com/?q=37.774929,-122.419418")
    assert drone is not None and drone.event_type == "drone_found"
    assert drone.data["mac"] == "aa:bb:cc:dd:ee:ff" and drone.data["rssi"] == -63
    assert drone.data["has_drone_location"] is True and drone.data["source"] == "mesh"
    # The Pilot line carries only the operator fix -> correlated onto the last Drone line's MAC.
    pilot = p.parse_line("Pilot: https://maps.google.com/?q=37.775100,-122.420000")
    assert pilot is not None and pilot.event_type == "drone_found"
    assert pilot.data["mac"] == "aa:bb:cc:dd:ee:ff"
    assert pilot.data["pilot_lat"] == 37.775100 and pilot.data["has_pilot_location"] is True


def test_mesh_pilot_line_with_no_preceding_drone_is_dropped():
    # A Pilot line with nothing to key it to must NOT manufacture a phantom drone.
    assert DroneMeshProtocol().parse_line(
        "Pilot: https://maps.google.com/?q=37.7,-122.4") is None


def test_identify():
    p = DroneMeshProtocol()
    assert p.identify(_FULL) is True
    assert p.identify(_HEARTBEAT) is True
    assert p.identify("Drone: aa:bb:cc:dd:ee:ff RSSI:-63") is True
    assert p.identify("some other firmware banner") is False
    assert p.identify('{"event":"detection","mac_address":"AA:BB"}') is False  # that's Flock-You's


def test_no_commands_passive_detector():
    assert DroneMeshProtocol().get_commands() == []


def test_drone_found_is_not_routed_into_the_shared_target_pool():
    # GUARD: the slice stays OFF the routing spine — target_ingest must NOT make a drone_found a
    # Target (no TargetType.DRONE, no _event_to_target branch), like nrf24/iot terminal events.
    ing = TargetIngestor(TargetPool(EventBus()))
    ev = ParsedEvent(event_type="drone_found",
                     data={"mac": "aa:bb:cc:dd:ee:ff", "basic_id": "X"}, raw=_FULL)
    assert ing._event_to_target(ev, "COM3") is None
