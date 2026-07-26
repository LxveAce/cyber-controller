"""Wave-10 Phase C (slice 1): the Operate Home tab is now wrapped in the shared PageLayout shell.

Parity guardrail: wrapping the primary dual-axis home in the frame must NOT lose any existing tab or
tool. Asserts every top-level tab still constructs, the Operate Home tab is now backed by a
PageLayout (with its status bar / posture / omnibar chrome), the inner OperateHome is preserved
inside it, and the tab-index references main_window relies on still resolve.
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


# The top-level tabs that must all survive the wrap (parity set).
_EXPECTED_TABS = {"Flash", "Connect", "Operate", "Operate Home", "Survey", "Analyze", "Settings"}


def test_all_top_level_tabs_survive_the_wrap(win):
    labels = {win._tabs.tabText(i) for i in range(win._tabs.count())}
    assert _EXPECTED_TABS <= labels, f"a tab was lost: missing {_EXPECTED_TABS - labels}"


def test_operate_home_tab_is_now_a_page_layout_frame(win):
    assert isinstance(win._home_frame, PageLayout)            # the tab widget is the shared shell
    assert win._tabs.indexOf(win._home_frame) >= 0           # resolvable as a tab widget
    win._tabs.setCurrentWidget(win._home_frame)               # the focus reference still works
    assert win._tabs.currentWidget() is win._home_frame


def test_inner_operate_home_is_preserved_inside_the_frame(win):
    from src.ui.qt.operate_home import OperateHome
    # _operate_home stays the real OperateHome (used elsewhere); it is the frame content.
    assert isinstance(win._operate_home, OperateHome)
    assert win._home_frame._content is win._operate_home
    # the domain grid centers main_window wired are still intact
    assert win._oh_wifi is not None and win._oh_ble is not None


def test_home_binder_wired_to_the_hub(win):
    # the binder exists and read the hub without error (the device-truth status slot is present)
    assert win._home_binder is not None
    assert "armed" in win._home_frame._status   # the status slot the binder drives exists
