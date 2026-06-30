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
