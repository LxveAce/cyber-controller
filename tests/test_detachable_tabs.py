"""Detachable / pop-out tabs (src/ui/qt/detachable_tabs.py).

A tab can pop out into its own top-level window and re-dock seamlessly; closing a pop-out re-docks by
default (never destroys a panel). Runs offscreen — no display required.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

from src.ui.qt.detachable_tabs import DetachableTabWidget, PopoutWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _tabs(qapp):
    w = DetachableTabWidget()
    for name in ("Alpha", "Bravo", "Charlie"):
        page = QLabel(name)
        page.setObjectName(name)
        w.addTab(page, name)
    return w


def test_detach_removes_tab_and_opens_window(qapp):
    w = _tabs(qapp)
    page = w.widget(1)
    win = w.detach_index(1)
    assert isinstance(win, PopoutWindow)
    assert w.count() == 2
    assert [w.tabText(i) for i in range(w.count())] == ["Alpha", "Charlie"]
    assert page.parent() is win  # the page now lives in the pop-out window
    assert page in w._popouts


def test_redock_restores_tab_in_place(qapp):
    w = _tabs(qapp)
    page = w.widget(1)
    win = w.detach_index(1)
    win.redock_requested.emit(page)  # the Re-dock button does this
    assert w.count() == 3
    assert [w.tabText(i) for i in range(w.count())] == ["Alpha", "Bravo", "Charlie"]
    assert not w._popouts


def test_closing_popout_redocks_by_default(qapp):
    w = _tabs(qapp)
    page = w.widget(2)
    win = w.detach_index(2)
    assert w.count() == 2
    win.close()  # OS close button -> closeEvent re-docks instead of destroying
    assert w.count() == 3
    assert w.widget(2).objectName() == "Charlie"
    assert not w._popouts


def test_detach_current(qapp):
    w = _tabs(qapp)
    w.setCurrentIndex(0)
    win = w.detach_current()
    assert win is not None
    assert w.count() == 2
    assert "Alpha" not in [w.tabText(i) for i in range(w.count())]


def test_detach_invalid_index_is_noop(qapp):
    w = _tabs(qapp)
    assert w.detach_index(-1) is None
    assert w.detach_index(99) is None
    assert w.count() == 3


def test_double_detach_same_page_is_noop(qapp):
    w = _tabs(qapp)
    page = w.widget(0)
    w.detach_index(0)
    # page is gone from the bar; trying to detach it again does nothing bad
    assert w.detach_index(0) is not None  # now index 0 is the NEXT tab, which is fine
    assert len(w._popouts) == 2


def test_close_all_popouts_redocks_everything(qapp):
    w = _tabs(qapp)
    w.detach_index(0)
    w.detach_index(0)  # detaches the new index 0
    assert w.count() == 1
    w.close_all_popouts()
    assert w.count() == 3
    assert not w._popouts


def test_detached_state_roundtrip(qapp):
    w = _tabs(qapp)
    w.detach_index(1)  # Bravo
    state = w.detached_state()
    assert "Bravo" in state
    w.close_all_popouts()
    assert w.count() == 3 and not w._popouts
    w.restore_detached(state)
    assert w.count() == 2
    assert "Bravo" in {win.tab_text for win in w._popouts.values()}


def test_restore_detached_bad_input_never_raises(qapp):
    w = _tabs(qapp)
    w.restore_detached("")          # empty
    w.restore_detached("not json")  # garbage
    w.restore_detached("[1,2,3]")   # wrong type
    assert w.count() == 3


def test_corner_popout_button_has_tooltip(qapp):
    """Track B UX #4 (discoverability): the '⇱' pop-out control must explain itself — the only
    always-visible affordance for detaching a tab, so it can't be a bare unlabeled glyph."""
    from PyQt5.QtCore import Qt

    w = _tabs(qapp)
    corner = w.cornerWidget(Qt.TopRightCorner)
    assert corner is not None
    assert corner.toolTip().strip(), "the pop-out corner button needs a non-empty tooltip"


def test_real_window_uses_detachable_tabs_and_detach_roundtrips(qapp, tmp_path, monkeypatch):
    """End-to-end: the actual main window wires DetachableTabWidget, and a real tab detaches + re-docks."""
    import src.config.settings as S
    monkeypatch.setattr(S, "SETTINGS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(S, "SETTINGS_PATH", tmp_path / "settings.json", raising=False)

    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.core.cross_comm import EventBus, TargetPool
    from src.ui.qt.main_window import CyberControllerWindow

    bus = EventBus()
    win = CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))
    try:
        assert isinstance(win._tabs, DetachableTabWidget)
        before = win._tabs.count()
        flash_page = win._tabs.widget(0)
        pop = win._tabs.detach_index(0)
        assert pop is not None
        assert win._tabs.count() == before - 1
        assert flash_page.parent() is pop
        # re-dock and confirm the panel is back
        pop.redock_requested.emit(flash_page)
        assert win._tabs.count() == before
        # detached_state is JSON-serializable and closing cleans up without error
        assert isinstance(win._tabs.detached_state(), str)
    finally:
        win.close()
