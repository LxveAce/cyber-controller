"""Perf: every tab's always-on refresh/poll timer runs only while the tab is visible.

The Broadcast / Wi-Fi / BLE tabs once started a 1s (analyzers) / 4s (broadcast) refresh timer in
__init__ and never stopped it, so a hidden tab kept recomputing off-screen forever. They now start
the timer on showEvent and stop it on hideEvent (like Operate/Health/Nodes/CrossComm), so a
background tab costs ~0. This locks that invariant across the tabs whose ``_timer`` is an always-on
repeater: re-arming one in __init__, or dropping the hideEvent stop, fails here.

Out of scope (correct by design): single-shot debounce timers (cross_comm ``_pool_refresh_timer``,
network/targets ``_refresh_timer``) are event-driven + self-stopping, and wardrive-multi's timer is
scan-lifecycle-gated. Offscreen Qt.
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


def _operate():
    from src.core.device_manager import DeviceManager
    from src.ui.qt.operate_tab import OperateTab
    return OperateTab(DeviceManager())


class _FakeNodesCtrl:
    def is_unlocked(self):
        return True

    def list_rows(self):
        return []


def _nodes():
    from src.ui.qt.nodes_tab import NodesTab
    return NodesTab(controller=_FakeNodesCtrl())


def _health():
    from src.core.health_monitor import HealthMonitor
    from src.ui.qt.health_tab import HealthTab
    return HealthTab(HealthMonitor())


@pytest.mark.parametrize("factory", [_broadcast, _wifi, _ble, _operate, _nodes, _health])
def test_refresh_timer_runs_only_while_visible(qapp, factory):
    tab = factory()
    assert not tab._timer.isActive()          # not running before the tab is ever shown
    tab.showEvent(QShowEvent())
    assert tab._timer.isActive()              # shown -> running
    tab.hideEvent(QHideEvent())
    assert not tab._timer.isActive()          # hidden -> stopped (a background tab costs ~0)
