"""Track B UX pass — empty-state guidance + core-control tooltips (presentation only).

Mirrors the empty-state assertion in test_network_tab.py: build a tab against an empty
manager/pool, assert the guidance placeholder is present, then assert it clears once real
data arrives. Also spot-checks that a few core controls carry non-empty tooltips. Offscreen Qt."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_device_list_empty_state_then_clears(qapp):
    from src.core.device_manager import DeviceManager
    from src.models.device import Device
    from src.ui.qt.device_tab import DeviceTab

    dm = DeviceManager()
    tab = DeviceTab(dm)  # __init__ calls _refresh_devices()

    # Empty pool -> exactly one non-selectable guidance row.
    assert tab._device_list.count() == 1
    hint = tab._device_list.item(0)
    assert "No devices" in hint.text()
    assert not (hint.flags() & Qt.ItemIsSelectable)  # placeholder cannot be selected

    # A real device replaces the placeholder.
    dm.add_device(Device(port="COM7", name="Marauder", firmware="marauder", connected=False))
    tab._refresh_devices()
    assert tab._device_list.count() == 1
    real = tab._device_list.item(0)
    assert "No devices" not in real.text()
    assert real.flags() & Qt.ItemIsSelectable


def test_device_tab_core_tooltips(qapp):
    from src.core.device_manager import DeviceManager
    from src.ui.qt.device_tab import DeviceTab

    tab = DeviceTab(DeviceManager())
    assert tab._btn_connect.toolTip()
    assert tab._btn_disconnect.toolTip()
    assert tab._btn_send.toolTip()
    assert tab._firmware_combo.toolTip()


def test_targets_empty_state_and_header_tooltips(qapp):
    from src.core.cross_comm import EventBus, TargetPool
    from src.models.target import Target, TargetType
    from src.ui.qt.targets_tab import TargetsTab

    bus = EventBus()
    pool = TargetPool(bus)
    tab = TargetsTab(pool, bus)  # __init__ calls _refresh()

    # Empty pool -> guidance visible (isHidden is reliable offscreen; isVisible needs a parent).
    assert not tab._empty_hint.isHidden()
    assert tab._empty_hint.text()

    # Abbreviated column headers carry explanatory tooltips.
    assert tab._table.horizontalHeaderItem(4).toolTip()  # "Ch"
    assert tab._table.horizontalHeaderItem(6).toolTip()  # "Enc"

    # A target hides the guidance.
    pool.add(Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP, ssid="Net",
                    channel=6, rssi=-40, device_source="COM7"))
    tab._refresh()
    assert tab._empty_hint.isHidden()
