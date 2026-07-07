"""Offscreen tests for the SignalBarsDelegate (RSSI column painter).

The delegate does ALL painting for a numeric-RSSI cell (it does not chain to super().paint on that
path), so it alone must draw the row-selection highlight on the RSSI column. These paint the cell onto
an offscreen QImage and assert the selection fill lands only when the cell is actually selected.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import QRect  # noqa: E402
from PyQt5.QtGui import (  # noqa: E402
    QColor,
    QImage,
    QPainter,
    QStandardItem,
    QStandardItemModel,
)
from PyQt5.QtWidgets import QApplication, QStyle, QStyleOptionViewItem  # noqa: E402

from src.ui.qt.widgets.signal_bars import SignalBarsDelegate  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


_SELECTION_FILL = QColor("#1c2128").rgb()


def _paint_cell(delegate: SignalBarsDelegate, *, selected: bool) -> QImage:
    """Paint one numeric-RSSI cell onto an opaque-black image and return it."""
    model = QStandardItemModel(1, 1)
    model.setItem(0, 0, QStandardItem("-57"))              # numeric text -> custom paint path
    index = model.index(0, 0)

    img = QImage(120, 40, QImage.Format_ARGB32)
    img.fill(QColor("#000000"))                            # distinct from the #1c2128 selection fill
    painter = QPainter(img)
    opt = QStyleOptionViewItem()
    opt.rect = QRect(0, 0, 120, 40)
    if selected:
        opt.state |= QStyle.State_Selected
    delegate.paint(painter, opt, index)
    painter.end()
    return img


def test_selected_rssi_cell_draws_selection_background(qapp):
    delegate = SignalBarsDelegate()
    # (2, 2) is the top-left corner of the cell — above the bars and left of the text, so it shows
    # only the selection fill (or the untouched background), never a bar/glyph.
    selected = _paint_cell(delegate, selected=True)
    unselected = _paint_cell(delegate, selected=False)

    # Selected: the delegate must paint the #1c2128 selection highlight so the RSSI cell matches the row.
    assert selected.pixel(2, 2) == _SELECTION_FILL
    # Unselected: no highlight — the corner stays the base background.
    assert unselected.pixel(2, 2) != _SELECTION_FILL
