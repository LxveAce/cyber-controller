"""Per-device Broadcast sections (Task 4) — reactive population + force-any-firmware. Offscreen."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core.cross_comm import EventBus, TargetPool  # noqa: E402
from src.core.cross_comm_hub import CrossCommHub  # noqa: E402
from src.core.device_manager import DeviceManager  # noqa: E402
from src.models.device import Device  # noqa: E402
from src.ui.qt.broadcast_tab import BroadcastBar  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _bar():
    dm = DeviceManager()
    bus = EventBus()
    hub = CrossCommHub(dm, bus, TargetPool(bus))
    return BroadcastBar(hub.broadcast, dm, bus), dm


def test_section_appears_and_disappears_with_device(qapp):
    bar, dm = _bar()
    assert bar._sections == {}
    dm.add_device(Device(port="COM7", firmware="marauder", connected=True))
    bar._rebuild_sections()
    assert "COM7" in bar._sections
    assert not bar._empty_hint.isVisible()
    dm.get_device("COM7").connected = False
    bar._rebuild_sections()
    assert "COM7" not in bar._sections


def test_force_firmware_updates_section_and_flag(qapp):
    bar, dm = _bar()
    dm.add_device(Device(port="COM7", firmware="marauder", connected=True))
    bar._rebuild_sections()
    # force to a different firmware — its command set becomes available on that device
    dm.set_firmware("COM7", "ghost-esp", forced=True)
    bar._rebuild_sections()
    dev = dm.get_device("COM7")
    assert dev.firmware == "ghost-esp" and dev.firmware_forced is True
    assert bar._engine.supported_verbs("ghost-esp")  # ghost-esp verbs now drive the section buttons


def test_per_device_launch_plans_only_that_port(qapp):
    from src.core.broadcast import BroadcastVerb
    bar, dm = _bar()
    dm.add_device(Device(port="COM7", firmware="marauder", connected=True))
    dm.add_device(Device(port="COM8", firmware="marauder", connected=True))
    plan = bar._engine.plan_for_port("COM7", BroadcastVerb.FIND_APS)
    assert [c.port for c in plan.concrete] == ["COM7"]  # single-device, not both
