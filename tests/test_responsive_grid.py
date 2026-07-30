"""Wave-3 adaptive GUI: the ResponsiveTileGrid delivers REAL 2-col/3-up (not a direction flip) and
the OPERATE HOME DomainGrid uses it.

These assert the CLAIM — the actual grid COLUMN COUNT and each tile's (row, col) cell at compact /
regular / expanded widths — so a passing test proves the columns→QGridLayout consumption, not merely
that a resize handler ran. Offscreen Qt; no window needed.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QLabel  # noqa: E402

from src.ui.qt.domain_grid import DomainGrid  # noqa: E402
from src.ui.qt.layout_profile import layout_profile  # noqa: E402
from src.ui.qt.widgets.responsive_grid import ResponsiveTileGrid  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _pos(g, tile):
    idx = g._grid.indexOf(tile)
    r, c, _rs, _cs = g._grid.getItemPosition(idx)
    return (r, c)


def test_reflow_tracks_columns_and_tile_positions(qapp):
    tiles = [QLabel(str(i)) for i in range(6)]
    g = ResponsiveTileGrid(tiles)
    for w, h in [(1600, 900), (860, 800), (420, 900)]:
        g.resize(w, h)
        qapp.processEvents()
        g._relayout_for_size()  # offscreen non-shown widgets don't auto-fire resizeEvent
        # Resolver on the ACTUAL geometry (robust to any minimum-size clamping the widget applies).
        cols = layout_profile(max(1, g.width()), max(1, g.height())).columns
        assert g.column_count() == cols
        # Tile 4 lands in the real grid cell (4 // cols, 4 % cols): (1,1) at 3-up, (2,0) at 2-col,
        # (4,0) at 1-col — distinct positions prove a true N-column reflow, not a stacked/row flip.
        assert _pos(g, tiles[4]) == (4 // cols, 4 % cols)


def test_expanded_is_three_up_and_compact_is_single_column(qapp):
    tiles = [QLabel(str(i)) for i in range(6)]
    g = ResponsiveTileGrid(tiles)
    g.resize(1600, 900)  # well inside the expanded band (>1024 ref-pt)
    qapp.processEvents()
    g._relayout_for_size()
    assert g.column_count() == 3
    assert _pos(g, tiles[3]) == (1, 0) and _pos(g, tiles[5]) == (1, 2)
    g.resize(420, 900)   # well inside the compact band (<600 ref-pt)
    qapp.processEvents()
    g._relayout_for_size()
    assert g.column_count() == 1
    assert _pos(g, tiles[3]) == (3, 0) and _pos(g, tiles[5]) == (5, 0)


def test_debounces_within_a_column_band(qapp):
    tiles = [QLabel(str(i)) for i in range(4)]
    g = ResponsiveTileGrid(tiles)
    g.resize(1600, 900)
    qapp.processEvents()
    g._relayout_for_size()
    assert g.column_count() == 3
    before = _pos(g, tiles[3])
    g.resize(1500, 880)  # still expanded — same column count, no re-lay needed
    qapp.processEvents()
    g._relayout_for_size()
    assert g.column_count() == 3
    assert _pos(g, tiles[3]) == before


# ── OPERATE HOME DomainGrid (uses the responsive engine) ──
def test_domain_grid_has_the_brief_domains(qapp):
    dg = DomainGrid()
    assert dg.domain_keys() == ["wifi", "ble", "subghz", "gps", "tools", "settings"]
    assert len(dg._cards) == 6
    assert isinstance(dg.grid, ResponsiveTileGrid)


def test_domain_grid_reflows_and_emits_the_selected_key(qapp):
    dg = DomainGrid()
    dg.grid.resize(1600, 900)   # expanded -> 3-up across the 6 domain tiles
    qapp.processEvents()
    dg.grid._relayout_for_size()
    assert dg.grid.column_count() == 3
    got: list[str] = []
    dg.domain_selected.connect(got.append)
    dg._cards["ble"].activated.emit()
    assert got == ["ble"]
