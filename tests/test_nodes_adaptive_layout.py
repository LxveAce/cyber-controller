"""Wave-3 Batch C: the Nodes action-button row reflows by DENSITY (screen 6/7).

The DECISION is the pure `nodes_layout` (unit-tested in test_layout_profile); here the widget
APPLIES it. Nodes is the density-driven screen: the six-button row is 6-wide when roomy, but on a
compact canvas it splits by input type — 1 column for touch (a tall 1-wide stack), 2 for pointer (a
3x2 grid). Offscreen Qt; the touch/pointer split at compact is the edge this pins.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt.layout_profile import layout_profile, nodes_layout  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _grid_columns_used(tab) -> int:
    g = tab._btn_grid
    cols = {g.getItemPosition(j)[1] for j in range(g.count())}
    return (max(cols) + 1) if cols else 0


@pytest.mark.parametrize("w,touch,columns", [
    (1440, False, 6),   # roomy   -> all six in a row
    (800, False, 6),    # regular -> still 6-wide (not compact)
    (480, False, 2),    # compact pointer -> 3x2 grid
    (480, True, 1),     # compact touch   -> 1-wide stack
])
def test_apply_nodes_layout_density(qapp, w, touch, columns):
    from src.ui.qt.nodes_tab import NodesTab
    tab = NodesTab()
    assert len(tab._buttons) == 6
    nl = nodes_layout(layout_profile(w, 800, touch=touch, dpi=96))
    assert nl.columns == columns   # the decider contract
    tab._apply_nodes_layout(nl)
    assert _grid_columns_used(tab) == columns
    assert tab._btn_grid.count() == 6   # every button placed
    assert all(b.minimumHeight() == nl.hit_edge_pt for b in tab._buttons)


def test_compact_touch_vs_pointer_split(qapp):
    # The divergence to pin: at the SAME compact width, touch stacks 1-wide, pointer goes 2.
    from src.ui.qt.nodes_tab import NodesTab
    tab = NodesTab()
    tab._apply_nodes_layout(nodes_layout(layout_profile(480, 800, touch=True, dpi=96)))
    assert _grid_columns_used(tab) == 1
    tab._apply_nodes_layout(nodes_layout(layout_profile(480, 800, touch=False, dpi=96)))
    assert _grid_columns_used(tab) == 2


def test_relayout_matches_the_resolver_and_debounces(qapp):
    from src.ui.qt.nodes_tab import NodesTab
    tab = NodesTab()
    for w in (400, 1600):
        tab.resize(w, 800)
        qapp.processEvents()
        tab._relayout_nodes()
        p = layout_profile(max(1, tab.width()), max(1, tab.height()), touch=False,
                           dpi=tab.logicalDpiX() or 96)
        assert _grid_columns_used(tab) == nodes_layout(p).columns
    first = tab._last_nodes_size
    tab._relayout_nodes()   # same size class -> no-op
    assert tab._last_nodes_size == first
