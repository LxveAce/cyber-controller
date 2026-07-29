"""Wave-3 (bespoke screens): the Broadcast bar's button grid reflows with the window size.

The Universal-broadcast grid used a magic ``cols = 4`` and fixed 64/48px button heights. It now
flows into a ``profile.columns``-wide grid (1/2/3) and sizes each button from the hit-target floor —
neither overflows a narrow deck nor ignores a touch target. Offscreen Qt; size-driven.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt import touch_mode as TM  # noqa: E402
from src.ui.qt.layout_profile import layout_profile  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _reset_touch():
    TM.set_touch_mode("off")     # deterministic pointer sizing for the assertions
    yield
    TM.set_touch_mode("auto")


def _bar():
    from src.core.broadcast import BroadcastEngine
    from src.core.cross_comm import EventBus
    from src.core.device_manager import DeviceManager
    from src.ui.qt.broadcast_tab import BroadcastBar
    dm, bus = DeviceManager(), EventBus()
    return BroadcastBar(BroadcastEngine(dm, bus), dm, bus)


def _grid_columns_used(bar) -> int:
    g = bar._verb_grid
    cols = {g.getItemPosition(j)[1] for j in range(g.count())}
    return (max(cols) + 1) if cols else 0


@pytest.mark.parametrize("w,columns", [(480, 1), (800, 2), (1440, 3)])
def test_grid_columns_track_the_profile(qapp, w, columns):
    bar = _bar()
    p = layout_profile(w, 800, touch=False, dpi=96)
    assert p.columns == columns
    bar._apply_broadcast_layout(p.columns, p.min_target_pt, p.is_compact)
    assert _grid_columns_used(bar) == columns
    assert bar._verb_grid.count() == len(bar._buttons)   # every verb button placed, none dropped


def test_buttons_never_shrink_below_the_hit_target(qapp):
    bar = _bar()
    p = layout_profile(480, 800, touch=True, dpi=96)     # compact + touch -> a real 44pt target
    bar._apply_broadcast_layout(p.columns, p.min_target_pt, p.is_compact)
    for b in bar._buttons.values():
        assert b.minimumHeight() >= p.min_target_pt
    assert bar._stop_btn.minimumHeight() >= p.min_target_pt


def test_relayout_matches_the_resolver_and_debounces(qapp):
    bar = _bar()
    for w in (400, 1600):
        bar.resize(w, 800)
        qapp.processEvents()
        bar._relayout_broadcast()
        p = layout_profile(max(1, bar.width()), max(1, bar.height()), touch=False,
                           dpi=bar.logicalDpiX() or 96)
        assert _grid_columns_used(bar) == p.columns
    first = bar._last_broadcast_size
    bar._relayout_broadcast()   # same size class -> no-op
    assert bar._last_broadcast_size == first
