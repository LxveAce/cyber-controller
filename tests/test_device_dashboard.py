"""DEVICE ▸ Dashboard (device_dashboard.py) — the reform landing screen composes the REAL HealthTab +
DeviceTab (+ optional Cross-Comm) by re-parenting them, so nothing from REFORM-DENSITY-SPEC's Dashboard
field set is dropped. Offscreen Qt; constructs the real widgets (no serial opened, no monitor started).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core.device_manager import DeviceManager  # noqa: E402
from src.ui.qt.device_dashboard import DeviceDashboard  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make():
    from src.core.health_monitor import HealthMonitor
    from src.ui.qt.device_tab import DeviceTab
    from src.ui.qt.health_tab import HealthTab
    dm = DeviceManager()
    health = HealthTab(HealthMonitor())   # constructed, NOT started — no poll thread
    dev = DeviceTab(dm)
    cc = None
    dash = DeviceDashboard(health, dev, cross_comm=cc)
    return dash, health, dev


def test_composes_health_and_devices_by_reparenting(qapp):
    dash, health, dev = _make()
    assert isinstance(dash, DeviceDashboard)
    # the REAL widgets are re-parented INTO the dashboard's splitter (density: nothing rebuilt/dropped)
    assert dash._split.indexOf(health) >= 0
    assert dash._split.indexOf(dev) >= 0
    assert dev.parent() is not None and health.parent() is not None


def test_device_pane_order(qapp):
    dash, health, dev = _make()
    # host health beside (index 0); the device control + terminal is the primary pane (index 1)
    assert dash._split.widget(0) is health
    assert dash._split.widget(1) is dev


def test_set_ui_mode_forwards_without_raising(qapp):
    dash, _health, _dev = _make()
    dash.set_ui_mode("simple")   # forwards to children that implement set_ui_mode; must not raise
    dash.set_ui_mode("pro")
