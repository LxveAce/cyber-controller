"""Wave-3 Batch C: the Multi-Wardrive tab's Boards + GPS/baud rows reflow with the window size.

The DECISION is the pure `wardrive_multi_layout` (unit-tested in test_layout_profile); here we
verify the widget APPLIES it — the two QHBoxLayouts flip stacked<->row on the compact edge — and
that the resize handler debounces on the size class. Offscreen Qt; size-driven (not the depth).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QBoxLayout  # noqa: E402

from src.ui.qt import wardrive_multi_tab as WM  # noqa: E402
from src.ui.qt.layout_profile import layout_profile, wardrive_multi_layout  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_apply_wm_layout_flips_both_rows(qapp):
    tab = WM.WardriveMultiTab()
    rows = (tab._boards_row, tab._gps_row)
    tab._apply_wm_layout(wardrive_multi_layout(layout_profile(480, 800)))    # compact -> stacked
    assert all(r.direction() == QBoxLayout.TopToBottom for r in rows)
    tab._apply_wm_layout(wardrive_multi_layout(layout_profile(1600, 900)))   # expanded -> row
    assert all(r.direction() == QBoxLayout.LeftToRight for r in rows)


def test_relayout_matches_the_resolver(qapp):
    tab = WM.WardriveMultiTab()
    for w, h in [(400, 800), (1600, 900)]:
        tab.resize(w, h)
        qapp.processEvents()
        tab._relayout_for_size()
        p = layout_profile(max(1, tab.width()), max(1, tab.height()), touch=False,
                           dpi=tab.logicalDpiX() or 96)
        stacked = wardrive_multi_layout(p).stack
        expected = QBoxLayout.TopToBottom if stacked else QBoxLayout.LeftToRight
        assert tab._boards_row.direction() == expected
        assert tab._last_wm_size is not None


def test_relayout_debounces_on_size_class(qapp):
    tab = WM.WardriveMultiTab()
    tab.resize(400, 800)
    qapp.processEvents()
    tab._relayout_for_size()
    first = tab._last_wm_size
    tab._relayout_for_size()   # same size class -> no re-apply, same recorded size
    assert tab._last_wm_size == first
