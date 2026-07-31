"""P3 flow-spine substrate — main_window.dispatch_intent routes + delivers (offscreen Qt).

Proves the dispatcher navigates to the destination verb surface + hands the object to the receive
method, WITHOUT arming or sending — the substrate the per-surface emitter slices (B/C/D) build on.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core.flow_intent import FlowIntent  # noqa: E402


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
def win(qapp):
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


def test_dispatch_to_crack_navigates_and_loads_the_capture(win):
    # A capture with a real file path -> dispatch focuses CRACK/Crack Lab AND load_capture stages it,
    # reusing the exact tested load path. LOAD-ONLY — no crack starts (consent gate is the arming point).
    from src.models.capture import CaptureRecord
    rec = CaptureRecord(bssid="aa:bb:cc:dd:ee:ff", capture_type="eapol", pcap_path="/tmp/hs.pcap")
    win._tabs.setCurrentWidget(win._settings_tab)   # start elsewhere so the jump is proven, not coincidental
    ok = win.dispatch_intent(FlowIntent("crack", "load_capture", rec, sub_view="crack_lab"))
    assert ok is True
    assert win._tabs.currentWidget() is win._crack_surface
    assert win._crack_surface.currentWidget() is win._crack_lab_tab
    assert win._crack_lab_tab._capture_edit.text() == "/tmp/hs.pcap"
    assert win._crack_lab_tab._active_capture_key == rec.key


def test_dispatch_to_operate_preselects_device_without_arming(win):
    # target -> Operate: dispatch focuses OPERATE/Control + pre-selects the device; arm stays SAFE.
    from src.models.device import Device
    dev = Device(port="COM15", firmware="marauder", connected=True)
    win._dm.add_device(dev)
    win._tabs.setCurrentWidget(win._settings_tab)
    ok = win.dispatch_intent(FlowIntent("operate", "select_device", "COM15", sub_view="control"))
    assert ok is True
    assert win._tabs.currentWidget() is win._operate_surface
    assert win._operate_surface.currentWidget() is win._operate_action
    assert win._operate_console._active_port == "COM15"   # pre-selected
    # PRE-SELECT ONLY — the hand-off never arms the device (SAFE/ARMED two-factor stays the gate).
    assert getattr(dev, "arm_state", "") != "armed"


def test_dispatch_unknown_or_absent_target_is_a_safe_noop(win):
    # Unknown surface (e.g. reserved 'sense') / unknown sub_view / no object_ref -> no crash, returns False.
    assert win.dispatch_intent(FlowIntent("sense", "detect", object(), sub_view="detect")) is False
    assert win.dispatch_intent(FlowIntent("crack", "load_capture", None, sub_view="nope")) is False


def test_dispatch_target_table_backs_only_real_receive_methods(win):
    # Each registered flow target's receive_widget must actually have its action method (no dead routes).
    assert hasattr(win._crack_lab_tab, "load_capture")
    assert hasattr(win._operate_console, "select_device")
    assert ("crack", "crack_lab") in win._flow_targets
    assert ("operate", "control") in win._flow_targets
