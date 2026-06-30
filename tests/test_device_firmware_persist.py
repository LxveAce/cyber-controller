"""Keystone bug-hunt fix (#6/#7): the device tab persists the selected firmware onto the Device, so the
ActionResolver + BroadcastEngine (which key off Device.firmware) actually resolve a protocol. Offscreen."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_persist_firmware_enables_resolver(qapp):
    from src.core.device_manager import DeviceManager
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.action_resolver import ActionResolver
    from src.models.device import Device
    from src.models.target import Target, TargetType
    from src.ui.qt.device_tab import DeviceTab

    dm = DeviceManager()
    dm.add_device(Device(port="COM7", name="dev", connected=True))
    tab = DeviceTab(dm, TargetPool(EventBus()), None)
    tab._active_port = "COM7"

    # Before persisting, Device.firmware is empty -> resolver can't key a protocol -> no actions.
    t = Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP, ssid="X", device_source="COM7")
    assert "COM7" not in ActionResolver(dm).resolve(t)

    # Persisting writes the selected firmware (default Auto-detect -> marauder) onto the Device.
    tab._persist_firmware()
    assert dm.get_device("COM7").firmware == "marauder"

    # Now the resolver resolves the device's protocol and returns its AP actions.
    resolved = ActionResolver(dm).resolve(t)
    assert resolved.get("COM7")


def test_persist_firmware_noop_when_not_connected(qapp):
    from src.core.device_manager import DeviceManager
    from src.core.cross_comm import EventBus, TargetPool
    from src.models.device import Device
    from src.ui.qt.device_tab import DeviceTab

    dm = DeviceManager()
    dm.add_device(Device(port="COM8", name="dev", connected=False))
    tab = DeviceTab(dm, TargetPool(EventBus()), None)
    tab._active_port = "COM8"
    tab._persist_firmware()  # not connected -> must not write firmware
    assert dm.get_device("COM8").firmware == ""
