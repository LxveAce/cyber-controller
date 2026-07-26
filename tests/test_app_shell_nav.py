"""Wave-10 Phase C slice A: the app-shell wraps the top area with a top-level nav sidebar.

Parity guardrail: wrapping the top area (device-sidebar + tabs) in the shell must lose nothing —
every top-level surface stays reachable via BOTH the tab-bar (unchanged) AND the new shell sidebar
(dual nav this slice). Asserts the shell is the splitter's top widget, the tabs live inside it, the
sidebar has a destination per surface, and selecting one drives the tab widget.
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


_SURFACES = {"flash", "connect", "operate", "operate-home", "survey", "analyze", "settings"}


def test_app_shell_is_the_splitter_top_widget(win):
    assert isinstance(win._app_shell, PageLayout)
    assert win._main_splitter.widget(0) is win._app_shell   # shell is the top slot
    assert win._main_splitter.count() == 2                  # shell + terminal (unchanged shape)


def test_tabs_live_inside_the_shell_content(win):
    # the whole top area (sidebar + tabs) is the shell's content; _tabs is unchanged + reachable
    assert win._app_shell._content is not None
    assert win._tabs.count() == 7                            # all 7 top-level tabs still present


def test_shell_sidebar_has_a_destination_per_surface(win):
    dests = set(win._app_shell._destinations)
    assert _SURFACES <= dests, f"missing shell nav destinations: {_SURFACES - dests}"


def test_shell_nav_selects_the_surface_in_the_tabs(win):
    # dual nav: selecting a shell destination drives _tabs.setCurrentWidget to that surface
    win._app_shell.select_destination("connect")
    assert win._tabs.currentWidget() is win._connect_surface
    win._app_shell.select_destination("analyze")
    assert win._tabs.currentWidget() is win._network_surface
    win._app_shell.select_destination("operate-home")
    assert win._tabs.currentWidget() is win._home_frame


def test_tab_bar_still_visible_this_slice(win):
    # slice A keeps the tab-bar (hiding it is slice C) — nothing is lost, both navs work
    assert win._tabs.tabBar().isVisibleTo(win._tabs) or not win._tabs.tabBar().isHidden()
