"""Golden-vector tests for the drone Remote-ID decoder (src/core/remote_id.py) — RX/decode only.

Vectors are built to match the drone-mesh-mapper firmware's own ``snprintf`` format strings
(colonelpanichacks/drone-mesh-mapper, ``remoteid-mesh/src/main.cpp``):

* USB Serial JSON::

    {"mac":"%s", "rssi":%d, "drone_lat":%.6f, "drone_long":%.6f, "drone_altitude":%d,
     "pilot_lat":%.6f, "pilot_long":%.6f, "basic_id":"%s"}

  plus a ``{"heartbeat":"Device is active and running."}`` keepalive.
* Meshtastic relay text: ``Drone: <mac> RSSI:<n>[ https://maps.google.com/?q=<lat>,<lon>]`` and
  ``Pilot: https://maps.google.com/?q=<lat>,<lon>``.

We assert the decoder recovers exactly the emitted fields, plus rejections (heartbeat, non-JSON,
malformed, no-mac). Nothing here transmits.
"""
from __future__ import annotations

from src.core.remote_id import RemoteIdDetection, parse_detection_json, parse_mesh_line

# A complete detection line exactly as the firmware emits it (spaces after commas, no space after
# the colon), with both drone and pilot positions broadcast.
_FULL = (
    '{"mac":"aa:bb:cc:dd:ee:ff", "rssi":-63, "drone_lat":37.774929, "drone_long":-122.419418, '
    '"drone_altitude":152, "pilot_lat":37.775100, "pilot_long":-122.420000, '
    '"basic_id":"1581F5XYZ0000000ABCD"}'
)
_HEARTBEAT = '{"heartbeat":"Device is active and running."}'


def test_full_json_detection_decodes_every_field():
    d = parse_detection_json(_FULL)
    assert isinstance(d, RemoteIdDetection)
    assert d.mac == "aa:bb:cc:dd:ee:ff"
    assert d.rssi == -63
    assert d.drone_lat == 37.774929
    assert d.drone_long == -122.419418
    assert d.drone_altitude == 152
    assert d.pilot_lat == 37.775100
    assert d.pilot_long == -122.420000
    assert d.basic_id == "1581F5XYZ0000000ABCD"
    assert d.has_drone_location is True
    assert d.has_pilot_location is True
    assert d.label == "1581F5XYZ0000000ABCD"  # basic_id wins when present


def test_mac_is_lowercased():
    d = parse_detection_json(_FULL.replace("aa:bb:cc:dd:ee:ff", "AA:BB:CC:DD:EE:FF"))
    assert d is not None and d.mac == "aa:bb:cc:dd:ee:ff"


def test_heartbeat_is_not_a_detection():
    assert parse_detection_json(_HEARTBEAT) is None


def test_non_json_line_is_rejected():
    assert parse_detection_json("Drone: aa:bb:cc:dd:ee:ff RSSI:-63") is None
    assert parse_detection_json("") is None
    assert parse_detection_json("   ") is None


def test_malformed_json_is_rejected():
    assert parse_detection_json('{"mac":"aa:bb", "rssi":}') is None  # invalid JSON
    assert parse_detection_json('{"rssi":-63}') is None              # no mac -> not a detection
    assert parse_detection_json('["mac","aa:bb"]') is None           # a list, not an object


def test_unset_coordinates_report_no_location():
    line = (
        '{"mac":"11:22:33:44:55:66", "rssi":-80, "drone_lat":0.000000, "drone_long":0.000000, '
        '"drone_altitude":0, "pilot_lat":0.000000, "pilot_long":0.000000, "basic_id":""}'
    )
    d = parse_detection_json(line)
    assert d is not None
    assert d.has_drone_location is False
    assert d.has_pilot_location is False
    assert d.label == "11:22:33:44:55:66"  # falls back to MAC when basic_id is empty


def test_missing_optional_fields_fall_back_to_firmware_defaults():
    d = parse_detection_json('{"mac":"de:ad:be:ef:00:01"}')
    assert d is not None
    assert d.rssi == 0
    assert d.basic_id == ""
    assert d.drone_lat == 0.0 and d.drone_long == 0.0
    assert d.drone_altitude == 0
    assert d.pilot_lat == 0.0 and d.pilot_long == 0.0
    assert d.has_drone_location is False


def test_detection_is_frozen():
    d = parse_detection_json(_FULL)
    assert d is not None
    try:
        d.rssi = 0  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("RemoteIdDetection should be immutable")


# --- Meshtastic relay text ---

def test_mesh_drone_line_with_maps_url():
    got = parse_mesh_line(
        "Drone: aa:bb:cc:dd:ee:ff RSSI:-63 https://maps.google.com/?q=37.774929,-122.419418"
    )
    assert got == {
        "kind": "drone",
        "mac": "aa:bb:cc:dd:ee:ff",
        "rssi": -63,
        "drone_lat": 37.774929,
        "drone_long": -122.419418,
    }


def test_mesh_drone_line_without_coordinates():
    got = parse_mesh_line("Drone: AA:BB:CC:DD:EE:FF RSSI:-70")
    assert got == {"kind": "drone", "mac": "aa:bb:cc:dd:ee:ff", "rssi": -70}


def test_mesh_pilot_line():
    got = parse_mesh_line("Pilot: https://maps.google.com/?q=37.775100,-122.420000")
    assert got == {"kind": "pilot", "pilot_lat": 37.775100, "pilot_long": -122.420000}


def test_mesh_non_matching_lines_are_rejected():
    assert parse_mesh_line("Pilot: (no fix yet)") is None
    assert parse_mesh_line("random log line") is None
    assert parse_mesh_line(_FULL) is None  # the JSON is not a mesh line
    assert parse_mesh_line("") is None
