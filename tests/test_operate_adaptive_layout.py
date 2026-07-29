"""Wave-3 Batch C: the Operate console reflows with the window size (screen 4/7).

The DECISION is the pure `operate_layout` (unit-tested in test_layout_profile); here we verify the
widget APPLIES it — the command grid's columns track `profile.columns` (was a hard-coded 3), the
device/firmware header stacks on a compact canvas, dense chrome shrinks the log, and the arm buttons
gain a real touch-target min-height — and that the resize handler debounces on the size class.
Offscreen Qt; size-driven (independent of the firmware/depth).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QBoxLayout, QGridLayout, QGroupBox  # noqa: E402

from src.models.device import Device  # noqa: E402
from src.ui.qt.layout_profile import layout_profile, operate_layout  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _FakeDM:
    def __init__(self, dev):
        self._dev = dev

    def list_devices(self):
        return [self._dev]

    def get_device(self, port):
        return self._dev if self._dev.port == port else None

    def get_connection(self, port):
        return None


def _tab_with_grid():
    """An Operate tab with a real (multi-button) command grid for a connected lxveos device."""
    from src.ui.qt.operate_tab import OperateTab
    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "safe"
    tab = OperateTab(_FakeDM(dev), dms_seen=set())
    tab._active_port = "COM23"
    tab._grid_fw = ""
    tab._refresh()
    return tab


def _grid_columns_used(tab) -> int:
    """Max column index (+1) used across the per-category command grids."""
    mx = -1
    for i in range(tab._grid_layout.count()):
        w = tab._grid_layout.itemAt(i).widget()
        if isinstance(w, QGroupBox) and isinstance(w.layout(), QGridLayout):
            g = w.layout()
            for j in range(g.count()):
                mx = max(mx, g.getItemPosition(j)[1])
    return mx + 1


@pytest.mark.parametrize("w,h,columns,stack,collapse", [
    (480, 800, 1, True, True),     # compact  -> single column, header stacked, dense log
    (800, 800, 2, False, False),   # regular  -> two columns
    (1440, 900, 3, False, False),  # expanded -> three columns
])
def test_apply_operate_layout(qapp, w, h, columns, stack, collapse):
    tab = _tab_with_grid()
    assert tab._tx_buttons or tab._safe_buttons, "lxveos must build a command grid"
    ol = operate_layout(layout_profile(w, h, touch=False, dpi=96))
    assert (ol.columns, ol.stack, ol.collapse_chrome) == (columns, stack, collapse)  # the contract
    tab._apply_operate_layout(ol)
    assert _grid_columns_used(tab) == columns
    stacked = tab._head_row.direction() == QBoxLayout.TopToBottom
    assert stacked == stack
    assert (tab._log.maximumHeight() < 120) == collapse   # dense chrome shrinks the log
    assert tab._btn_arm.minimumHeight() == ol.hit_edge_pt  # real touch target


def test_grid_buttons_get_the_hit_target(qapp):
    tab = _tab_with_grid()
    tab._apply_operate_layout(operate_layout(layout_profile(1440, 900, touch=True, dpi=96)))
    hit = tab._hit_edge_pt
    assert hit >= 44   # touch profile -> a generous min target
    assert all(b.minimumHeight() == hit for b in (tab._tx_buttons + tab._safe_buttons))


def test_relayout_matches_the_resolver(qapp):
    tab = _tab_with_grid()
    for w, h in [(400, 800), (1600, 900)]:
        tab.resize(w, h)
        qapp.processEvents()
        tab._relayout_operate()
        p = layout_profile(max(1, tab.width()), max(1, tab.height()), touch=False,
                           dpi=tab.logicalDpiX() or 96)
        assert _grid_columns_used(tab) == operate_layout(p).columns
        assert tab._last_operate_size is not None


def test_relayout_debounces_on_size_class(qapp):
    tab = _tab_with_grid()
    tab.resize(400, 800)
    qapp.processEvents()
    tab._relayout_operate()
    first = tab._last_operate_size
    tab._relayout_operate()   # same size class -> no re-apply, same recorded size
    assert tab._last_operate_size == first
