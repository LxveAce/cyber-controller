"""DeviceManager.set_firmware — the single authoritative firmware setter + its change event (Task 4).

The per-device Broadcast overhaul (force-any-firmware + reactive per-device sections) needs one setter
that centralizes the firmware write and notifies subscribers, and a ``firmware_forced`` flag a manual
choice can ride so re-autodetect doesn't clobber it.
"""
from __future__ import annotations

from src.core.device_manager import DeviceManager
from src.models.device import Device


def test_set_firmware_updates_and_fires_change():
    dm = DeviceManager()
    dm.add_device(Device(port="COM9"))
    seen: list = []
    dm.on_device_changed(seen.append)
    assert dm.set_firmware("COM9", "marauder", forced=True) is True
    dev = dm.get_device("COM9")
    assert dev.firmware == "marauder"
    assert dev.firmware_forced is True
    assert len(seen) == 1 and seen[0] is dev


def test_set_firmware_missing_port_returns_false():
    dm = DeviceManager()
    assert dm.set_firmware("NOPE", "marauder") is False


def test_set_firmware_no_change_no_event():
    dm = DeviceManager()
    dm.add_device(Device(port="COM9", firmware="marauder", firmware_forced=True))
    seen: list = []
    dm.on_device_changed(seen.append)
    assert dm.set_firmware("COM9", "marauder", forced=True) is True  # identical -> no change
    assert seen == []


def test_forced_flag_can_be_cleared_for_autodetect():
    dm = DeviceManager()
    dm.add_device(Device(port="COM9", firmware="marauder", firmware_forced=True))
    dm.set_firmware("COM9", "", forced=False)  # user picked "Auto-detect"
    dev = dm.get_device("COM9")
    assert dev.firmware == "" and dev.firmware_forced is False
