"""Status consolidation: the ONE shell owns transient toasts + the folded-in status chrome.

Before this, fleeting "action ran / failed" notices went to a second `QMainWindow.statusBar()` — a
duplicate bottom bar beside the shell's top status bar. Now `PageLayout.toast()` is the single
transient surface (distinct from the persistent `set_status`/`set_badge` slots), the system-health
label + mode badge fold into the one shell bar, and the tab call sites route through `window.toast`.
Offscreen Qt.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QLabel  # noqa: E402

from src.ui.qt.page_layout import PageLayout  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_toast_shows_and_clears(qapp):
    p = PageLayout()
    assert p._toast_label.isHidden()
    p.toast("hi", timeout=0)                 # timeout<=0 -> persists, no timer
    assert not p._toast_label.isHidden()
    assert p._toast_label.text() == "hi"
    assert not p._toast_timer.isActive()
    p._clear_toast()
    assert p._toast_label.isHidden()


def test_toast_timeout_arms_the_autoclear_timer(qapp):
    p = PageLayout()
    p.toast("bye", timeout=3000)
    assert p._toast_timer.isActive()


def test_toast_level_tints_the_text(qapp):
    p = PageLayout()
    p.toast("boom", level="error")
    assert "#f85149" in p._toast_label.styleSheet()   # error red
    p.toast("ok", level="success")
    assert "#3fb950" in p._toast_label.styleSheet()   # success green


def test_add_status_widget_folds_into_the_shell_bar(qapp):
    p = PageLayout()
    before = p._status_bar_layout.count()
    w = QLabel("CPU 5%")
    p.add_status_widget(w)
    assert p._status_bar_layout.count() == before + 1
    # inserted before the omnibar (the last item), i.e. not at the very end
    assert p._status_bar_layout.itemAt(p._status_bar_layout.count() - 1).widget() is not w


def _make_window():
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt.main_window import CyberControllerWindow
    bus = EventBus()
    return CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))


def test_window_folds_status_chrome_into_the_shell_and_toasts(qapp):
    win = _make_window()
    try:
        shell = win._app_shell
        bar = shell._status_bar_layout
        widgets = {bar.itemAt(i).widget() for i in range(bar.count())}
        assert win._status_label in widgets     # system-health line folded into the shell bar
        assert win._mode_badge in widgets        # mode badge folded in (no separate bottom bar)
        win.toast("action ran", timeout=0)
        assert shell._toast_label.text() == "action ran"
        assert not shell._toast_label.isHidden()
    finally:
        win.close()


def test_tab_notify_routes_to_window_toast(qapp):
    # The migrated call sites gate on hasattr(window, "toast") and route there instead of a bottom
    # statusBar().showMessage — verify the tab's _notify reaches a host toast.
    from src.core.cross_comm import EventBus, TargetPool
    from src.ui.qt.targets_tab import TargetsTab

    bus = EventBus()
    tab = TargetsTab(TargetPool(bus), bus)
    seen = []

    class _Host:
        def toast(self, msg, level="info", timeout=4000):
            seen.append((msg, level, timeout))

    tab.window = lambda: _Host()   # type: ignore[assignment]
    tab._notify("heads up")
    assert seen == [("heads up", "info", 4000)]
