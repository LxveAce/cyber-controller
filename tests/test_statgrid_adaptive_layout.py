"""Wave-3 (bespoke screens): the shared StatGrid + the Wi-Fi/BLE analyzers wrap on a compact canvas.

The Biscuit `StatGrid` built a fixed N-wide tile row that overflows a narrow deck. It now exposes
`set_columns()`, and both analyzer tabs drive it from their layout profile — one shared fix, reused
twice: the 6-wide Wi-Fi row and 5-wide BLE row wrap to 3 (regular) / 2 (compact) columns. Offscreen.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt import touch_mode as TM  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _reset_touch():
    TM.set_touch_mode("off")
    yield
    TM.set_touch_mode("auto")


def _cols(sg) -> int:
    g = sg._grid
    return max((g.getItemPosition(j)[1] for j in range(g.count())), default=-1) + 1


def test_statgrid_set_columns_reflows_and_keeps_every_tile(qapp):
    from src.ui.qt.biscuit import StatGrid
    sg = StatGrid(["a", "b", "c", "d", "e", "f"], columns=6)
    assert _cols(sg) == 6
    sg.set_columns(2)
    assert _cols(sg) == 2
    sg.set_columns(3)
    assert _cols(sg) == 3
    assert sg._grid.count() == 6           # no tile dropped across reflows


@pytest.mark.parametrize("width,cols", [(480, 2), (800, 3), (1400, 6)])
def test_wifi_analyzer_statgrid_wraps(qapp, width, cols):
    from src.ui.qt.wifi_analyzer_tab import WifiAnalyzerTab
    tab = WifiAnalyzerTab()
    tab.resize(width, 700)
    qapp.processEvents()
    tab._last_wifi_size = None
    tab._relayout_wifi()
    assert _cols(tab._stats) == cols


@pytest.mark.parametrize("width,cols", [(480, 2), (800, 3), (1400, 5)])
def test_ble_analyzer_statgrid_wraps(qapp, width, cols):
    from src.ui.qt.ble_analyzer_tab import BleAnalyzerTab
    tab = BleAnalyzerTab()
    tab.resize(width, 700)
    qapp.processEvents()
    tab._last_ble_size = None
    tab._relayout_ble()
    assert _cols(tab._stats) == cols


def test_wifi_relayout_debounces(qapp):
    from src.ui.qt.wifi_analyzer_tab import WifiAnalyzerTab
    tab = WifiAnalyzerTab()
    tab.resize(400, 700)
    qapp.processEvents()
    tab._relayout_wifi()
    first = tab._last_wifi_size
    tab._relayout_wifi()   # same size class -> no-op
    assert tab._last_wifi_size == first
