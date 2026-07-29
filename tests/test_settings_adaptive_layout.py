"""Wave-3 Batch C: the Settings tab's cards reflow into a responsive 1/2/3-column grid.

The DECISION is the pure `settings_layout` (unit-tested in test_layout_profile); here we verify the
widget APPLIES it — the cards land in a `profile.columns`-wide grid, dense chrome demotes the helper
text, the resize handler debounces on the size class, and Simple mode compacts the grid around the
cards it hides. Offscreen Qt; size-driven (independent of the Simple/Pro depth).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt import settings_tab as ST  # noqa: E402
from src.ui.qt.layout_profile import layout_profile, settings_layout  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _grid_columns_used(tab) -> int:
    grid = tab._cards_grid
    cols = {grid.getItemPosition(i)[1] for i in range(grid.count())}
    return (max(cols) + 1) if cols else 0


@pytest.mark.parametrize("w,h,columns,collapse", [
    (480, 800, 1, True),    # compact  -> single column, dense chrome
    (800, 800, 2, False),   # regular  -> two columns
    (1440, 900, 3, False),  # expanded -> three columns
])
def test_apply_settings_layout_reflows_grid(qapp, w, h, columns, collapse):
    tab = ST.SettingsTab()
    sl = settings_layout(layout_profile(w, h, touch=False, dpi=96))
    assert (sl.columns, sl.collapse_chrome) == (columns, collapse)   # the decider contract
    tab._apply_settings_layout(sl)
    assert _grid_columns_used(tab) == columns
    assert tab._cards_grid.count() == len(tab._cards)   # every card is placed, none dropped
    # Dense chrome demotes the long helper descriptions (never a functional control).
    assert all(lbl.isHidden() == collapse for lbl in tab._settings_muted_labels)


def test_relayout_matches_the_resolver(qapp):
    tab = ST.SettingsTab()
    for w, h in [(400, 800), (1600, 900)]:
        tab.resize(w, h)
        qapp.processEvents()
        tab._relayout_settings()
        p = layout_profile(max(1, tab.width()), max(1, tab.height()), touch=False,
                           dpi=tab.logicalDpiX() or 96)
        assert _grid_columns_used(tab) == settings_layout(p).columns
        assert tab._last_settings_size is not None


def test_relayout_debounces_on_size_class(qapp):
    tab = ST.SettingsTab()
    tab.resize(400, 800)
    qapp.processEvents()
    tab._relayout_settings()
    first = tab._last_settings_size
    tab._relayout_settings()   # same size class -> no re-apply, same recorded size
    assert tab._last_settings_size == first


def test_simple_mode_compacts_the_grid(qapp):
    # Simple mode hides the six advanced cards; the grid must re-flow to place only the basic ones
    # that remain (Serial, Flash, Interface, Updates), not leave gaps where the hidden cards were.
    tab = ST.SettingsTab()
    tab._apply_settings_layout(settings_layout(layout_profile(1440, 900)))
    total = len(tab._cards)
    assert tab._cards_grid.count() == total
    tab.set_ui_mode("simple")
    assert tab._cards_grid.count() == total - 6   # six advanced cards hidden
    tab.set_ui_mode("pro")
    assert tab._cards_grid.count() == total
