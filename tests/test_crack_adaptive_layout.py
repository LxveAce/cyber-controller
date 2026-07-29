"""Wave-3 Batch C: the Crack Lab reflows with the window size (screen 5/7).

The DECISION is the pure `crack_layout` (unit-tested in test_layout_profile); here the widget
APPLIES it. Crack diverges: its controls/captures split goes side-by-side ONLY on a true desktop
and STACKS otherwise — `stack = not is_expanded`, the 1024 breakpoint, NOT the 600 compact edge
(the panels need the width). So a *regular* 800px window still stacks. Offscreen Qt.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt.layout_profile import crack_layout, layout_profile  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _tab():
    from src.ui.qt.crack_lab_tab import CrackLabTab
    return CrackLabTab(hub=None)


@pytest.mark.parametrize("w,stack", [
    (480, True),    # compact  -> stacked
    (800, True),    # regular  -> STILL stacked (the divergence: 600 edge does NOT split it)
    (1440, False),  # expanded -> side-by-side controls/captures
])
def test_apply_crack_layout_split(qapp, w, stack):
    tab = _tab()
    cl = crack_layout(layout_profile(w, 800, touch=False, dpi=96))
    assert cl.stack == stack   # the decider contract
    tab._apply_crack_layout(cl)
    expected = Qt.Vertical if stack else Qt.Horizontal
    assert tab._split.orientation() == expected
    assert tab._run_btn.minimumHeight() == cl.hit_edge_pt   # touch target


def test_split_flips_exactly_at_the_1024_edge(qapp):
    # Atlas's ask: pin the 1023/1024 boundary — Crack keys off is_expanded (>=1024), not is_compact.
    tab = _tab()
    tab._apply_crack_layout(crack_layout(layout_profile(1023, 800, touch=False, dpi=96)))
    assert tab._split.orientation() == Qt.Vertical      # 1023 = still stacked
    tab._apply_crack_layout(crack_layout(layout_profile(1024, 800, touch=False, dpi=96)))
    assert tab._split.orientation() == Qt.Horizontal     # 1024 = side-by-side


def test_dense_chrome_caps_the_captures_table(qapp):
    tab = _tab()
    tab._apply_crack_layout(crack_layout(layout_profile(480, 800, touch=False, dpi=96)))   # dense
    capped = tab._captures_table.maximumHeight()
    tab._apply_crack_layout(crack_layout(layout_profile(1440, 900, touch=False, dpi=96)))  # roomy
    assert capped < tab._captures_table.maximumHeight()


def test_relayout_matches_the_resolver_and_debounces(qapp):
    tab = _tab()
    for w in (400, 1600):
        tab.resize(w, 800)
        qapp.processEvents()
        tab._relayout_crack()
        p = layout_profile(max(1, tab.width()), max(1, tab.height()), touch=False,
                           dpi=tab.logicalDpiX() or 96)
        expected = Qt.Vertical if crack_layout(p).stack else Qt.Horizontal
        assert tab._split.orientation() == expected
    first = tab._last_crack_size
    tab._relayout_crack()   # same size class -> no-op, same recorded size
    assert tab._last_crack_size == first
