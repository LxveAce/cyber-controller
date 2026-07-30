"""Generic DOMAIN DETAIL frame — the shared master/detail used by a radio domain, so the dual-axis
frame is general, not a per-domain one-off.

CENTER: the live object table (injected). RIGHT: a detail widget for the selected object (injected;
must implement ``set_object(obj)`` + ``clear()``).

Responsive: the panels sit side-by-side on a roomy canvas, stacked vertically on a compact one; the
right detail collapses (hidden until an object is selected) when cramped — from the pure
``layout_profile().columns`` resolver. Presentation only — the host wires behaviour.

Spade v2 (P2c): the old left "Passive/Active" posture panel is gone — it was authorization THEATER
(the Active button never flipped posture, its signal was wired nowhere in production, and it implied
a safety boundary that does not exist). The real consent floor is ``safety.classify`` + the
OPERATE two-factor arm gate in ``operate_tab._send`` — untouched by this.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QBoxLayout,
    QHBoxLayout,
    QWidget,
)

from src.ui.qt.layout_profile import layout_profile


class DomainDetailView(QWidget):
    """Master/detail: a live object table (center) + a detail widget for the selected object."""

    object_selected = pyqtSignal(object)

    def __init__(self, center: QWidget, detail: QWidget,
                 parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._selected = None
        self._cols = 0
        self._center = center
        self._detail = detail

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(self._center, 3)
        row.addWidget(self._detail, 2)
        self._row = row
        self._relayout_for_size()

    # ── selection -> detail ──
    def select_object(self, obj) -> None:
        self._selected = obj
        self._detail.set_object(obj)
        self.object_selected.emit(obj)
        self._apply_layout()  # a selection reveals the detail on a cramped canvas

    def clear_selection(self) -> None:
        self._selected = None
        self._detail.clear()
        self._apply_layout()

    @property
    def detail(self) -> QWidget:
        return self._detail

    # ── responsive ──
    def columns(self) -> int:
        return self._cols

    def detail_visible(self) -> bool:
        """The right detail is shown on a roomy (expanded) canvas, or whenever an object is picked;
        on a cramped canvas with nothing selected it collapses."""
        return self._cols >= 3 or self._selected is not None

    def is_stacked(self) -> bool:
        """True on a compact canvas — the three panels stack vertically instead of side-by-side."""
        return self._cols == 1

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._relayout_for_size()

    def _relayout_for_size(self) -> None:
        self._cols = layout_profile(max(1, self.width()), max(1, self.height())).columns
        self._apply_layout()

    def _apply_layout(self) -> None:
        self._detail.setVisible(self.detail_visible())
        _dir = QBoxLayout.TopToBottom if self.is_stacked() else QBoxLayout.LeftToRight
        self._row.setDirection(_dir)
