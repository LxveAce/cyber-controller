"""Wave-3 (bespoke screens): the Flock heatmap's control rows stack on a compact canvas.

The file (4 buttons), live-scan, and map (8 checkboxes/buttons) control strips overflow a deck.
They now flip to a vertical stack on `is_compact` (horizontal otherwise), size-driven and debounced.
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
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab
    return FlockHeatmapTab()


def _rows(tab):
    return (tab._file_row, tab._live_row, tab._map_row)


def test_control_rows_stack_on_compact_and_row_otherwise(qapp):
    tab = _tab()
    tab.resize(480, 800)
    qapp.processEvents()
    tab._last_flock_size = None
    tab._relayout_flock()
    assert all(r.direction() == QBoxLayout.TopToBottom for r in _rows(tab))
    tab.resize(1200, 800)
    qapp.processEvents()
    tab._relayout_flock()
    assert all(r.direction() == QBoxLayout.LeftToRight for r in _rows(tab))


def test_relayout_matches_the_resolver_and_debounces(qapp):
    tab = _tab()
    for w in (400, 1400):
        tab.resize(w, 800)
        qapp.processEvents()
        tab._relayout_flock()
        p = layout_profile(max(1, tab.width()), max(1, tab.height()), touch=False,
                           dpi=tab.logicalDpiX() or 96)
        expected = QBoxLayout.TopToBottom if p.is_compact else QBoxLayout.LeftToRight
        assert tab._file_row.direction() == expected
    first = tab._last_flock_size
    tab._relayout_flock()   # same size class -> no-op
    assert tab._last_flock_size == first
