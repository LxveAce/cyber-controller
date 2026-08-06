"""DEVICE ▸ Dashboard (device_dashboard.py) — the reform landing re-homes the REAL live widgets out of
HealthTab / DeviceTab into a Primer card grid, so nothing from REFORM-DENSITY-SPEC's Dashboard field set
is dropped and the host logic still drives them. Offscreen Qt; constructs the real widgets (no serial
opened, no HealthMonitor thread started).
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
    dash = DeviceDashboard(health, dev, cross_comm=None)
    return dash, health, dev


def test_rehomes_live_widgets_into_the_dashboard(qapp):
    dash, health, dev = _make()
    assert isinstance(dash, DeviceDashboard)
    # the REAL live widgets are re-parented INTO the dashboard subtree (density: nothing rebuilt/dropped)
    assert dash.isAncestorOf(health._cpu_gauge)      # a host gauge now lives in the dashboard
    assert dash.isAncestorOf(dev._terminal)          # the serial terminal is re-homed
    assert dash.isAncestorOf(dev._device_list)       # the device list is re-homed
    assert dash.isAncestorOf(dev._bj_panel)          # the ungated BlueJammer STOP rides inside, whole


def test_selected_device_rendered_fresh_and_readable(qapp):
    # the Selected Device readouts are BUILT FRESH (not re-homed) so they can be styled readably;
    # the ARM/SAFE lamp is a fresh prominent pill and rendering must not raise (dev=None here).
    dash, _health, _dev = _make()
    assert dash.isAncestorOf(dash._sd_arm)           # fresh ARM/SAFE lamp present in the dashboard
    assert dash._sd_arm.text()                       # rendered (defaults to a lamp glyph, never blank)
    dash._render_selected_device()                   # re-render with no active device — must not raise


def test_pumps_host_refreshes(qapp):
    # the hosts are headless, so the dashboard owns timers that pump their refresh (else widgets freeze)
    dash, _health, _dev = _make()
    assert dash._health_timer.interval() == 5000
    assert dash._dev_timer.interval() == 3000


def test_responsive_reflow_for_small_screens(qapp):
    # CC is universal (cyberdeck 320-480px ... desktop): the 3 columns must stack on a narrow deck.
    from PyQt5.QtWidgets import QBoxLayout
    dash, _health, _dev = _make()
    dash._apply_responsive(1400)
    assert dash._stacked is False and dash._grid.direction() == QBoxLayout.LeftToRight
    dash._apply_responsive(400)
    assert dash._stacked is True and dash._grid.direction() == QBoxLayout.TopToBottom


def test_set_ui_mode_forwards_without_raising(qapp):
    dash, _health, _dev = _make()
    dash.set_ui_mode("simple")
    dash.set_ui_mode("pro")
