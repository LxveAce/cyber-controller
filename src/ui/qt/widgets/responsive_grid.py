"""Responsive tile grid — lays a fixed set of tiles into a ``QGridLayout`` whose COLUMN COUNT tracks
the window size (1 compact / 2 regular / 3 expanded), from the pure ``layout_profile().columns``
resolver.

Part of the adaptive GUI rebuild: the column DECISION is the Qt-free resolver (unit-tested
headless in ``test_layout_profile``); this widget only maps a column count to grid positions and
reflows on resize. It debounces on the column count, so it re-lays only when the count actually
changes. One shared engine delivers the real 2-col/3-up cases for the OPERATE HOME domain grid and
any other tile surface — not a per-widget re-implementation.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtWidgets import QGridLayout, QWidget

from src.ui.qt.layout_profile import layout_profile


class ResponsiveTileGrid(QWidget):
    """Reflow ``tiles`` across ``layout_profile().columns`` grid columns, adapting to the widget's
    width. ``column_count()`` reports the live column count; the tiles are re-parented into the grid
    at ``(index // cols, index % cols)`` each time the count changes."""

    def __init__(self, tiles, spacing: int = 12, parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._tiles: list[QWidget] = list(tiles)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(spacing)
        self._cols = 0
        self._relayout(self._resolve_columns())

    def _resolve_columns(self) -> int:
        dpi = self.logicalDpiX() or 96
        prof = layout_profile(max(1, self.width()), max(1, self.height()), touch=False, dpi=dpi)
        return prof.columns

    def column_count(self) -> int:
        """The current number of grid columns (1 / 2 / 3)."""
        return self._cols

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._relayout_for_size()

    def _relayout_for_size(self) -> None:
        cols = self._resolve_columns()
        if cols != self._cols:  # debounce: re-lay only when the column count actually changes
            self._relayout(cols)

    def _relayout(self, cols: int) -> None:
        cols = max(1, cols)
        self._cols = cols
        for tile in self._tiles:
            self._grid.removeWidget(tile)
        for i, tile in enumerate(self._tiles):
            self._grid.addWidget(tile, i // cols, i % cols)
