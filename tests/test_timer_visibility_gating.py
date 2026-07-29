"""Perf: the Broadcast / Wi-Fi / BLE tabs' refresh timers run only while the tab is visible.

These three tabs previously started a 1s (analyzers) / 4s (broadcast) refresh timer in __init__ and
never stopped it, so a hidden tab kept recomputing + repainting off-screen forever. They now start
the timer on showEvent and stop it on hideEvent (like Operate/Health/Nodes), so a background tab
costs ~0. Offscreen Qt.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtGui import QHideEvent, QShowEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _broadcast():
    from src.core.broadcast import BroadcastEngine
    from src.core.cross_comm import EventBus
    from src.core.device_manager import DeviceManager
    from src.ui.qt.broadcast_tab import BroadcastBar
    dm, bus = DeviceManager(), EventBus()
    return BroadcastBar(BroadcastEngine(dm, bus), dm, bus)


def _wifi():
    from src.ui.qt.wifi_analyzer_tab import WifiAnalyzerTab
    return WifiAnalyzerTab()


def _ble():
    from src.ui.qt.ble_analyzer_tab import BleAnalyzerTab
    return BleAnalyzerTab()


@pytest.mark.parametrize("factory", [_broadcast, _wifi, _ble])
def test_refresh_timer_runs_only_while_visible(qapp, factory):
    tab = factory()
    assert not tab._timer.isActive()          # not running before the tab is ever shown
    tab.showEvent(QShowEvent())
    assert tab._timer.isActive()              # shown -> running
    tab.hideEvent(QHideEvent())
    assert not tab._timer.isActive()          # hidden -> stopped (a background tab costs ~0)
