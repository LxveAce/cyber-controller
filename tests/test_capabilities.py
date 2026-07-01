"""Per-protocol capability declarations + the Devices-tab capability view (network-integration layer)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


def test_protocols_declare_capabilities():
    from src.protocols import get_protocol
    assert set(get_protocol("marauder").capabilities) == {"wifi", "ble", "gps", "deauth"}
    assert set(get_protocol("bw16").capabilities) == {"wifi", "deauth"}
    assert set(get_protocol("flipper").capabilities) == {"subghz", "nfc", "rfid", "ir", "ble", "badusb"}
    assert set(get_protocol("meshtastic").capabilities) == {"lora", "mesh"}


def test_capabilities_for_helper():
    from src.protocols import capabilities_for
    assert "wifi" in capabilities_for("marauder")
    assert "deauth" in capabilities_for("bw16")
    assert capabilities_for("does-not-exist") == frozenset()


def test_line_ending_for_helper():
    from src.protocols import line_ending_for
    assert line_ending_for("flipper") == "\r"   # Flipper CLI submits on CR
    assert line_ending_for("marauder") == "\n"
    assert line_ending_for("does-not-exist") == "\n"  # safe LF default


def test_protocols_declare_driver_type():
    from src.protocols import get_protocol
    # Line-shell firmwares are text-cli; Meshtastic is a protobuf stream; BlueJammer is web-UI control-map.
    assert get_protocol("marauder").driver_type == "text-cli"
    assert get_protocol("bruce").driver_type == "text-cli"
    assert get_protocol("meshtastic").driver_type == "stream"
    assert get_protocol("bluejammer").driver_type == "controlmap"


def test_driver_type_for_helper():
    from src.protocols import driver_type_for
    assert driver_type_for("meshtastic") == "stream"
    assert driver_type_for("bluejammer") == "controlmap"
    assert driver_type_for("marauder") == "text-cli"
    assert driver_type_for("does-not-exist") == "text-cli"  # safe default


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PyQt5.QtWidgets")
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_devices_tab_surfaces_capabilities(qapp):
    from src.core.device_manager import DeviceManager
    from src.ui.qt.device_tab import DeviceTab
    tab = DeviceTab(DeviceManager())
    # Default firmware (Auto-detect -> Marauder) -> WiFi/BLE/GPS/Deauth chips.
    txt = tab._caps_label.text().lower()
    assert "capabilities" in txt and "wifi" in txt and "deauth" in txt


def test_network_tab_is_honest_about_no_command_channel(qapp):
    # The Network tab must not present a stream/control-map device as an empty text CLI: the "nothing to
    # send" note should explain WHY (protobuf stream / web-UI control), driven off Device.driver_type.
    from src.core.device_manager import DeviceManager
    from src.models.device import Device
    from src.ui.qt.network_tab import NetworkTab
    tab = NetworkTab(DeviceManager())

    mesh_note = tab._device_actions(Device(port="COM_M", firmware="meshtastic"))[0][0].lower()
    assert "stream" in mesh_note

    jam_note = tab._device_actions(Device(port="COM_J", firmware="bluejammer"))[0][0].lower()
    assert "web ui" in jam_note

    # A text-cli firmware with genuinely no commands keeps the plain note (no false "stream" claim).
    plain = tab._device_actions(Device(port="COM_G", firmware="generic"))[0][0].lower()
    assert "stream" not in plain and "web ui" not in plain
