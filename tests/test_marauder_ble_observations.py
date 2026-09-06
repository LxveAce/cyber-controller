"""Passive scan formats grounded in Marauder 91724fd (v1.15.1).

WiFiScan.cpp prints RSSI + Device: + (name OR address). CommandLine.cpp's
list -b prints index/RSSI + that same ambiguous label, not a separate address.
"""
import pytest

from src.protocols.marauder import MarauderProtocol


@pytest.mark.parametrize("line,label,index", [
    ("-61 Device: Synthetic Watch", "Synthetic Watch", None),
    ("-61 Device: 02:00:00:00:00:01", "02:00:00:00:00:01", None),
    ("[0][RSSI:-61] Synthetic Watch", "Synthetic Watch", 0),
    ("[12][RSSI:-61] 02:00:00:00:00:01", "02:00:00:00:00:01", 12),
])
def test_official_ambiguous_labels_are_observations_not_device_addresses(line, label, index):
    event = MarauderProtocol().parse_line(line)
    assert event.event_type == "ble_observation"
    assert event.data == {"label": label, "rssi": -61, "reported_index": index,
                          "format": "list" if index is not None else "live",
                          "label_truncated": False, "addressable": False}
    assert "mac" not in event.data and "addr" not in event.data


@pytest.mark.parametrize("prefix", ["-61 Device: ", "[0][RSSI:-61] "])
@pytest.mark.parametrize("label", [
    "AP: invented BSSID: 02:00:00:00:00:02 Ch: 6 RSSI: -40",
    "Client: 02:00:00:00:00:02 AP: 02:00:00:00:00:03",
    "Handshake captured for 02:00:00:00:00:02",
])
def test_firmware_name_field_cannot_be_reinterpreted_as_another_record(prefix, label):
    parser = MarauderProtocol()
    event = parser.parse_line(prefix + label)
    assert event.event_type == "ble_observation"
    assert event.data["label"] == label
    assert parser._ap_indices == {}


def test_long_label_is_bounded_with_explicit_truncation():
    event = MarauderProtocol().parse_line("-61 Device: " + "x" * 64000)
    assert event.event_type == "ble_observation"
    assert event.data["label"] == "x" * 256
    assert event.data["label_truncated"] is True


def test_legacy_explicit_address_keeps_existing_target_contract():
    event = MarauderProtocol().parse_line("BLE: 02:00:00:00:00:01 Name: Watch RSSI: -61")
    assert event.event_type == "ble_found"
    assert event.data == {"mac": "02:00:00:00:00:01", "name": "Watch", "rssi": -61}


def test_real_wifi_scan_still_uses_wifi_path():
    event = MarauderProtocol().parse_line("AP: Lab BSSID: 02:00:00:00:00:02 Ch: 6 RSSI: -45")
    assert event.event_type == "ap_found" and event.data["index"] == 0
