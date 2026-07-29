"""Wave-3 adaptive layout: the Flash tab's top row reflows with the window size.

The layout DECISION is unit-tested in test_layout_profile (the pure ``flash_layout``); here we
verify the widget APPLIES it — the top row's QBoxLayout direction flips stacked<->row — and that
the resize handler tracks the resolver + debounces on the size class. Offscreen Qt; no real flash.
The reflow is size-driven only; it must never touch the user's Simple/Pro depth.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QBoxLayout  # noqa: E402

from src.core.device_manager import DeviceManager  # noqa: E402
from src.core.flash_engine import FlashEngine  # noqa: E402
from src.ui.qt import flash_tab as FT  # noqa: E402
from src.ui.qt.layout_profile import flash_layout, layout_profile  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _expected_dir(tab):
    prof = layout_profile(max(1, tab.width()), max(1, tab.height()), touch=False,
                          dpi=tab.logicalDpiX() or 96)
    return QBoxLayout.TopToBottom if flash_layout(prof).stack_top_row else QBoxLayout.LeftToRight


def test_apply_flash_layout_flips_direction(qapp):
    tab = FT.FlashTab(DeviceManager(), FlashEngine())
    rows = (tab._top_row, tab._bottom_row, tab._vault_row)
    tab._apply_flash_layout(flash_layout(layout_profile(480, 800)))   # compact -> all rows stacked
    assert all(r.direction() == QBoxLayout.TopToBottom for r in rows)
    tab._apply_flash_layout(flash_layout(layout_profile(1600, 900)))  # expanded -> all horizontal
    assert all(r.direction() == QBoxLayout.LeftToRight for r in rows)


def test_relayout_matches_the_resolver(qapp):
    tab = FT.FlashTab(DeviceManager(), FlashEngine())
    for w, h in [(400, 800), (1600, 900)]:
        tab.resize(w, h)
        qapp.processEvents()
        tab._relayout_for_size()
        # The applied direction must match what the resolver says for the tab's ACTUAL width
        # (robust to any minimum-size clamping the widget applies).
        assert tab._top_row.direction() == _expected_dir(tab)
        assert tab._last_flash_size is not None


def test_relayout_debounces_on_size_class(qapp):
    tab = FT.FlashTab(DeviceManager(), FlashEngine())
    tab.resize(400, 800)
    qapp.processEvents()
    tab._relayout_for_size()
    first = tab._last_flash_size
    # Calling again at the same size class is a no-op — same recorded size, still valid, no error.
    tab._relayout_for_size()
    assert tab._last_flash_size == first
    assert tab._top_row.direction() == _expected_dir(tab)
