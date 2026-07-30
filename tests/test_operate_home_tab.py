"""Wave-10 Phase C (slice D): the Operate Home tab holds OperateHome directly (frame reconciled).

Slices 1-2 wrapped OperateHome in a per-tab PageLayout (_home_frame) to prove the frame + chrome
worked in the app. Slice A then made the app-shell (_app_shell) wrap the WHOLE top area, so that
per-tab frame's global chrome (status bar / posture / omnibar) became redundant. Slice D reconciled
it away: the Operate Home tab now holds the real OperateHome, and OperateHome's OWN domain grid is
the Operate content nav (the radio axis of the two-level IA). This guards that reconcile — no tab or
tool lost, the global chrome lives once on the app-shell, and the domain grid still navigates.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt.page_layout import PageLayout  # noqa: E402


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


def _quiesce(win):
    try:
        win._health.stop()
    except Exception:  # noqa: BLE001
        pass
    for t in win.findChildren(QTimer):
        t.stop()


@pytest.fixture
def win(qapp):
    w = _make_window()
    _quiesce(w)
    yield w
    try:
        w.close()
    except Exception:  # noqa: BLE001
        pass
    w.deleteLater()
    qapp.processEvents()


# The top-level tabs that must all survive the reconcile (parity set).
_EXPECTED_TABS = {"Flash", "Connect", "Operate", "Operate Home", "Survey", "Analyze", "Settings"}


def test_all_top_level_tabs_survive_the_reconcile(win):
    labels = {win._tabs.tabText(i) for i in range(win._tabs.count())}
    assert _EXPECTED_TABS <= labels, f"a tab was lost: missing {_EXPECTED_TABS - labels}"


def test_operate_home_tab_is_the_operate_home_directly(win):
    from src.ui.qt.operate_home import OperateHome
    # the tab widget IS the real OperateHome — no per-tab PageLayout wrapper anymore.
    assert isinstance(win._operate_home, OperateHome)
    assert win._tabs.indexOf(win._operate_home) >= 0        # resolvable as a tab widget
    assert not isinstance(win._operate_home, PageLayout)    # the frame wrapper is gone
    win._tabs.setCurrentWidget(win._operate_home)            # the focus reference still works
    assert win._tabs.currentWidget() is win._operate_home
    # Spade v2 P2c: no duplicate analyzer clones — wifi/ble navigate to the real analyzer instead.
    assert not hasattr(win, "_oh_wifi") and not hasattr(win, "_oh_ble")


def test_no_stray_home_frame_or_binder(win):
    # the reconciled state owns the redundant wrapper + its binder — they must be gone.
    assert not hasattr(win, "_home_frame")
    assert not hasattr(win, "_home_binder")


def test_global_chrome_lives_once_on_the_app_shell(win):
    # the status bar / posture / omnibar are owned by the ONE app-shell that wraps the whole top
    # area, not duplicated per-tab. The Operate Home tab no longer carries its own copy.
    assert isinstance(win._app_shell, PageLayout)
    assert "armed" in win._app_shell._status               # the device-truth status slot exists
    assert win._app_shell._posture_lbl is not None          # the global posture indicator exists


def test_operate_home_grid_navigates_domains(win):
    # OperateHome's OWN domain grid is the Operate content nav. Selecting an IN-PLACE
    # domain (gps — wifi/ble are external now, they navigate away) drives its stack to that screen;
    # the back button returns to the grid.
    assert "gps" in win._operate_home._grid.domain_keys()
    win._operate_home.show_domain("gps")
    assert win._operate_home.current_domain() == "gps"
    win._operate_home.show_home()
    assert win._operate_home.current_domain() is None       # back to the grid
