"""Wave-3 Batch C: the Macro tab's left/right splitter + control rows reflow with the window size.

The DECISION is the pure `macro_layout` (unit-tested in test_layout_profile); here we verify the
widget APPLIES it — the QSplitter flips vertical on compact and the button/variable rows wrap — and
that the resize handler debounces on the size class. Offscreen Qt; size-driven (not the depth).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QBoxLayout  # noqa: E402

from src.core.device_manager import DeviceManager  # noqa: E402
from src.core.macro_recorder import MacroRecorder  # noqa: E402
from src.ui.qt import macro_tab as MT  # noqa: E402
from src.ui.qt.layout_profile import layout_profile, macro_layout  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_tab():
    return MT.MacroTab(MacroRecorder(), DeviceManager())


def test_apply_macro_layout_flips_splitter_and_rows(qapp):
    tab = _make_tab()
    tab._apply_macro_layout(macro_layout(layout_profile(480, 800)))    # compact
    assert tab._splitter.orientation() == Qt.Vertical
    assert tab._btn_row.direction() == QBoxLayout.TopToBottom
    assert tab._var_row.direction() == QBoxLayout.TopToBottom

    tab._apply_macro_layout(macro_layout(layout_profile(1440, 900)))   # expanded
    assert tab._splitter.orientation() == Qt.Horizontal
    assert tab._btn_row.direction() == QBoxLayout.LeftToRight
    assert tab._var_row.direction() == QBoxLayout.LeftToRight


def test_relayout_matches_the_resolver(qapp):
    tab = _make_tab()
    for w, h in [(400, 800), (1600, 900)]:
        tab.resize(w, h)
        qapp.processEvents()
        tab._relayout_for_size()
        p = layout_profile(max(1, tab.width()), max(1, tab.height()), touch=False,
                           dpi=tab.logicalDpiX() or 96)
        expected = Qt.Vertical if macro_layout(p).stack else Qt.Horizontal
        assert tab._splitter.orientation() == expected
        assert tab._last_macro_size is not None


def test_relayout_debounces_on_size_class(qapp):
    tab = _make_tab()
    tab.resize(400, 800)
    qapp.processEvents()
    tab._relayout_for_size()
    first = tab._last_macro_size
    tab._relayout_for_size()   # same size class -> same recorded size, no error
    assert tab._last_macro_size == first
