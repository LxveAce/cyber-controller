"""Wave-3 (bespoke screens): the Cross-Comm tab's Event-Stream / Auto-Rules row stacks on compact.

The Live Event Stream and Auto-Routing Rules cards sat side-by-side in a fixed horizontal row that
cramps on a narrow deck. They now flip to a vertical stack on `is_compact` (horizontal otherwise),
size-driven and debounced. Offscreen Qt.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QBoxLayout  # noqa: E402

from src.ui.qt import touch_mode as TM  # noqa: E402
from src.ui.qt.layout_profile import layout_profile  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _reset_touch():
    TM.set_touch_mode("off")
    yield
    TM.set_touch_mode("auto")


def _tab():
    from src.core.cross_comm import AutoRouter, EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.ui.qt.cross_comm_tab import CrossCommTab
    bus = EventBus()
    return CrossCommTab(bus, TargetPool(bus), AutoRouter(bus, lambda p, c: None), DeviceManager())


def test_stream_rules_row_stacks_on_compact(qapp):
    tab = _tab()
    tab.resize(480, 800)
    qapp.processEvents()
    tab._last_cc_size = None
    tab._relayout_cross_comm()
    assert tab._bottom_row.direction() == QBoxLayout.TopToBottom
    tab.resize(1200, 800)
    qapp.processEvents()
    tab._relayout_cross_comm()
    assert tab._bottom_row.direction() == QBoxLayout.LeftToRight


def test_relayout_matches_the_resolver_and_debounces(qapp):
    tab = _tab()
    for w in (400, 1400):
        tab.resize(w, 800)
        qapp.processEvents()
        tab._relayout_cross_comm()
        p = layout_profile(max(1, tab.width()), max(1, tab.height()), touch=False,
                           dpi=tab.logicalDpiX() or 96)
        expected = QBoxLayout.TopToBottom if p.is_compact else QBoxLayout.LeftToRight
        assert tab._bottom_row.direction() == expected
    first = tab._last_cc_size
    tab._relayout_cross_comm()   # same size class -> no-op
    assert tab._last_cc_size == first
