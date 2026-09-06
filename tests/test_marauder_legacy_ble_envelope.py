"""The explicit-address BLE record owns its literal name and final RSSI field."""
import pytest

from src.protocols.marauder import MarauderProtocol

ADDRESS = "02:00:00:00:00:01"


def record(name, rssi=-60, address=ADDRESS):
    return f"BLE: {address} Name: {name} RSSI: {rssi}"


@pytest.mark.parametrize("name", [
    "Client: 02:00:00:00:00:02 AP: 02:00:00:00:00:03",
    "Handshake captured for 02:00:00:00:00:03",
    "EAPOL found for 02:00:00:00:00:03",
    "SSID: Inert BSSID: 02:00:00:00:00:02 Ch: 6 RSSI: -40",
    "Watch RSSI: -20",
    "BLE: 02:00:00:00:00:02 Name: Inner RSSI: -40",
], ids=["client", "handshake", "eapol", "ap", "name-rssi", "nested-ble"])
def test_name_is_literal_and_final_rssi_belongs_to_outer_record(name):
    parser = MarauderProtocol()
    event = parser.parse_line(record(name))
    assert event is not None and event.event_type == "ble_found"
    assert event.data == {"mac": ADDRESS, "name": name, "rssi": -60}
    assert parser._ap_indices == {}


@pytest.mark.parametrize("name", ["", "Watch", "x" * 256, "é" * 256],
                         ids=["empty", "ordinary", "max-ascii", "max-unicode"])
@pytest.mark.parametrize("rssi", [-128, 127])
def test_bounded_names_including_unnamed_device_keep_explicit_address(name, rssi):
    event = MarauderProtocol().parse_line(record(name, rssi))
    assert event is not None and event.event_type == "ble_found"
    assert event.data == {"mac": ADDRESS, "name": name, "rssi": rssi}


@pytest.mark.parametrize("line", [
    record("Client: 02:00:00:00:00:02 AP: 02:00:00:00:00:03", address="broken"),
    record("Handshake captured for 02:00:00:00:00:03", address="00:00:00:00:00000"),
    record("Handshake captured for 02:00:00:00:00:03", address=":" * 17),
    record("Handshake captured for 02:00:00:00:00:03", rssi=-129),
    record("Handshake captured for 02:00:00:00:00:03", rssi=128),
    record("Handshake captured for 02:00:00:00:00:03", rssi="-60junk"),
    record("Handshake captured for 02:00:00:00:00:03", rssi="9" * 2000),
    record("Handshake captured for 02:00:00:00:00:03") + " trailer",
    f"BLE: {ADDRESS} Handshake captured for 02:00:00:00:00:03 RSSI: -60",
    f"BLE: {ADDRESS} Name: Handshake captured for 02:00:00:00:00:03",
    record("x" * 257),
    record("x" * 257 + " Client: 02:00:00:00:00:02 AP: 02:00:00:00:00:03"),
    record("x" * 64000 + " Handshake captured for 02:00:00:00:00:03"),
    record("Watch\nClient: 02:00:00:00:00:02 AP: 02:00:00:00:00:03"),
], ids=["bad-mac", "bad-octets", "colon-only-mac", "rssi-under", "rssi-over", "rssi-junk",
        "rssi-huge", "suffix-junk", "no-name-field", "no-rssi", "long-name", "long-name-client",
        "huge-name-handshake", "embedded-newline"])
def test_malformed_clear_ble_envelope_cannot_fall_through_to_inner_events(line):
    parser = MarauderProtocol()
    assert parser.parse_line(line) is None
    assert parser._ap_indices == {}
    # Rejecting one envelope must not suppress a later genuine record.
    event = parser.parse_line("Handshake captured for 02:00:00:00:00:03")
    assert event is not None and event.event_type == "handshake_captured"


@pytest.mark.parametrize("line,event_type", [
    ("AP: BLE: 02:00:00:00:00:01 Name: X BSSID: 02:00:00:00:00:02 Ch: 6 RSSI: -40", "ap_found"),
    ("-52 Ch: 6 02:00:00:00:00:02 ESSID: BLE: Name 11 15", "ap_found"),
    ("Client: 02:00:00:00:00:02 AP: 02:00:00:00:00:03", "client_found"),
    ("Handshake captured for 02:00:00:00:00:03", "handshake_captured"),
    ("-61 Device: Handshake captured for 02:00:00:00:00:03", "ble_observation"),
    ("[0][RSSI:-61] Client: 02:00:00:00:00:02 AP: 02:00:00:00:00:03", "ble_observation"),
])
def test_genuine_other_records_keep_their_own_outer_type(line, event_type):
    event = MarauderProtocol().parse_line(line)
    assert event is not None and event.event_type == event_type


def test_existing_whitespace_compatibility_does_not_change_address_or_name():
    event = MarauderProtocol().parse_line(" \tBLE:02:AA:BB:CC:DD:EE\tName: Watch band\tRSSI:-60\r\n")
    assert event is not None and event.event_type == "ble_found"
    assert event.data == {"mac": "02:AA:BB:CC:DD:EE", "name": "Watch band", "rssi": -60}


@pytest.mark.parametrize("separator", [" " * 930, "\t" * 930, " \t" * 465],
                         ids=["spaces", "tabs", "mixed"])
@pytest.mark.parametrize("suffix", ["RSSI: -60", "RSSI: -60junk", "Handshake captured for " + ADDRESS],
                         ids=["valid", "bad-signal", "no-signal"])
def test_large_separator_runs_preserve_empty_name_and_reject_malformed_suffix(separator, suffix):
    parser = MarauderProtocol()
    event = parser.parse_line(f"BLE: {ADDRESS} Name:" + separator + suffix)
    if suffix == "RSSI: -60":
        assert event is not None and event.event_type == "ble_found"
        assert event.data == {"mac": ADDRESS, "name": "", "rssi": -60}
    else:
        assert event is None
    assert parser._ap_indices == {}


@pytest.mark.parametrize("name,expected", [
    ("\t " * 200 + "x" * 256 + " \t" * 50, "x" * 256),
    ("\v" + "x" * 255, "x" * 255),
    ("\v" + "x" * 256, None),
    ("Watch RSSI: -20\tName: Handshake captured for " + ADDRESS,
     "Watch RSSI: -20\tName: Handshake captured for " + ADDRESS),
], ids=["name-separators", "nonseparator-counts", "nonseparator-over-limit", "literal-fields"])
def test_name_limit_excludes_only_separator_spaces_and_tabs(name, expected):
    event = MarauderProtocol().parse_line(record(name))
    if expected is None:
        assert event is None
    else:
        assert event is not None and event.event_type == "ble_found"
        assert event.data == {"mac": ADDRESS, "name": expected, "rssi": -60}


def test_final_rssi_requires_separator_even_for_empty_name():
    parser = MarauderProtocol()
    assert parser.parse_line(f"BLE: {ADDRESS} Name:RSSI:-60") is None
    assert parser.parse_line(f"BLE: {ADDRESS} Name:WatchRSSI:-60") is None
    event = parser.parse_line(f"BLE: {ADDRESS} Name:\tRSSI:-60")
    assert event is not None and event.event_type == "ble_found"
    assert event.data == {"mac": ADDRESS, "name": "", "rssi": -60}
