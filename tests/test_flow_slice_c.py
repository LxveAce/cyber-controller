"""P3 flow-spine Slice C (target->OPERATE): the Targets/HUNT list opens the OPERATE console with
a target's discovering device pre-selected.

Emitter (TargetsTab.operate_device_requested) + main_window._on_operate_device_requested, on Atlas's
Slice A FlowIntent substrate. NAVIGATION-only: it drives the OPERATE picker combo via
OperateTab.select_device; the two-factor arm gate stays the single arming point - nothing armed
or sent. Offscreen.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_window():
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt.main_window import CyberControllerWindow
    bus = EventBus()
    return CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))


@pytest.fixture
def win(qapp, tmp_path, monkeypatch):
    import src.core.install as _install
    monkeypatch.setattr(_install, "captures_dir", lambda: tmp_path)   # isolate the app store
    w = _make_window()
    try:
        w._health.stop()
    except Exception:  # noqa: BLE001
        pass
    for t in w.findChildren(QTimer):
        t.stop()
    yield w
    try:
        w.close()
    except Exception:  # noqa: BLE001
        pass
    w.deleteLater()
    qapp.processEvents()


def _target(port, mac="00:11:22:33:44:55"):
    from src.models.target import Target, TargetType
    return Target(mac=mac, target_type=TargetType.AP, ssid="LabAP", device_source=port)


def test_slice_c_operate_this_device_selects_it_in_the_console(win):
    # A target on COM3 -> "Operate this device" -> OPERATE console shown with COM3 selected.
    from src.models.device import Device
    win._dm.add_device(Device(port="COM3", firmware="lxveos", connected=True))
    win._show_subtab(win._crack_surface, win._crack_lab_tab)   # start on a DIFFERENT surface
    assert win._tabs.currentWidget() is win._crack_surface
    win._targets_tab.operate_device_requested.emit(_target("COM3"))
    # navigated to the OPERATE surface + the console sub-view
    assert win._tabs.currentWidget() is win._operate_surface
    assert win._operate_surface.currentWidget() is win._operate_action
    # NAVIGATION-only: the picker combo is now on COM3, and the device was never armed/sent.
    assert win._operate_console._active_port == "COM3"
    assert getattr(win._dm.get_device("COM3"), "arm_state", "") in ("", "safe")


def test_slice_c_unknown_device_navigates_but_selects_nothing(win):
    # A target whose device_source isn't registered -> select_device returns False (no crash);
    # the console is still shown but the combo is not set to the phantom port.
    win._targets_tab.operate_device_requested.emit(_target("COMZZ"))
    assert win._operate_console._active_port != "COMZZ"


def test_slice_c_no_device_source_does_not_dispatch(win):
    # A target with no discovering device -> the handler no-ops on the empty port (no crash/nav).
    before = win._tabs.currentWidget()
    win._targets_tab.operate_device_requested.emit(_target(""))
    assert win._tabs.currentWidget() is before
