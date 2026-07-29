"""Wave-3 (bespoke screens): the Health tab's gauge + detail rows stack on a compact canvas.

The four arc gauges (CPU/RAM/Disk/Battery) and their detail labels sat in fixed horizontal rows that
cram four-across on a narrow deck. They now flip to a vertical stack on `is_compact` (horizontal
otherwise), size-driven and debounced. Offscreen Qt.
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
    from src.core.health_monitor import HealthMonitor
    from src.ui.qt.health_tab import HealthTab
    return HealthTab(HealthMonitor())


def test_rows_stack_on_compact_and_row_otherwise(qapp):
    tab = _tab()
    tab._apply_health_layout(compact=True)
    assert tab._gauge_row.direction() == QBoxLayout.TopToBottom
    assert tab._detail_row.direction() == QBoxLayout.TopToBottom
    tab._apply_health_layout(compact=False)
    assert tab._gauge_row.direction() == QBoxLayout.LeftToRight
    assert tab._detail_row.direction() == QBoxLayout.LeftToRight


def test_relayout_matches_the_resolver_and_debounces(qapp):
    tab = _tab()
    for w in (400, 1400):
        tab.resize(w, 800)
        qapp.processEvents()
        tab._relayout_health()
        p = layout_profile(max(1, tab.width()), max(1, tab.height()), touch=False,
                           dpi=tab.logicalDpiX() or 96)
        expected = QBoxLayout.TopToBottom if p.is_compact else QBoxLayout.LeftToRight
        assert tab._gauge_row.direction() == expected
    first = tab._last_health_size
    tab._relayout_health()   # same size class -> no-op
    assert tab._last_health_size == first
