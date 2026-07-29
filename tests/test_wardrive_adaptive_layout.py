"""Wave-3 (bespoke screens): the Wardrive Serial-ports + Output cards go 2-up on a roomy canvas.

The two config cards were stacked vertically; they now sit side-by-side (2-up) with width and stack
on a compact deck, with the controls + log staying full-width below. Size-driven, debounced.
Offscreen Qt.
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
    from src.ui.qt.wardrive_tab import WardriveTab
    return WardriveTab()


def test_cards_go_2up_when_roomy_and_stack_on_compact(qapp):
    tab = _tab()
    assert tab._cards_row.count() == 2   # Serial-ports + Output both in the row
    tab.resize(480, 800)
    qapp.processEvents()
    tab._last_wd_size = None
    tab._relayout_wardrive()
    assert tab._cards_row.direction() == QBoxLayout.TopToBottom   # compact -> stacked
    tab.resize(1200, 800)
    qapp.processEvents()
    tab._relayout_wardrive()
    assert tab._cards_row.direction() == QBoxLayout.LeftToRight   # roomy -> 2-up


def test_relayout_matches_the_resolver_and_debounces(qapp):
    tab = _tab()
    for w in (400, 1400):
        tab.resize(w, 800)
        qapp.processEvents()
        tab._relayout_wardrive()
        p = layout_profile(max(1, tab.width()), max(1, tab.height()), touch=False,
                           dpi=tab.logicalDpiX() or 96)
        expected = QBoxLayout.TopToBottom if p.is_compact else QBoxLayout.LeftToRight
        assert tab._cards_row.direction() == expected
    first = tab._last_wd_size
    tab._relayout_wardrive()   # same size class -> no-op
    assert tab._last_wd_size == first
