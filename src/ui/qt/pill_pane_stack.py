"""PillPaneStack — a reform destination surface: a SegmentedPills row over a QStackedWidget (W1).

The drop-in replacement for ``main_window._verb_surface``'s inner ``QTabWidget`` — the reform's
"never a QTabWidget strip". The pills switch the stacked panes; nothing scrolls or overflows (the IA
caps a destination at <= 4 panes). A ``None`` pane widget is skipped (honest-empty: an unavailable
analyzer never becomes a blank pane), mirroring the old ``_verb_surface`` behaviour.

Structure only — it navigates panes, owns no screen content and no send path.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QStackedWidget, QVBoxLayout, QWidget

from src.ui.qt.segmented_pills import SegmentedPills


class PillPaneStack(QWidget):
    """A destination's pill sub-nav + its panes. ``pane_changed`` fires whenever the active pane
    changes (a user pill click OR a programmatic :meth:`select`)."""

    pane_changed = pyqtSignal(str)

    def __init__(self, parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._keys: "list[str]" = []
        self._pills = SegmentedPills()
        self._stack = QStackedWidget()
        self._pills.segment_selected.connect(self.select)   # user click -> switch + re-emit
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        v.addWidget(self._pills)
        v.addWidget(self._stack, 1)

    def set_panes(self, panes: "list[tuple[str, str, Optional[QWidget]]]") -> None:
        """(Re)build from ``(key, label, widget)`` triples. A ``None`` widget is skipped. The first
        pane is shown without emitting."""
        while self._stack.count():
            self._stack.removeWidget(self._stack.widget(0))
        self._keys = []
        segments: "list[tuple[str, str]]" = []
        for key, label, widget in panes:
            if widget is None:
                continue
            self._keys.append(key)
            segments.append((key, label))
            self._stack.addWidget(widget)
        self._pills.set_segments(segments)
        if self._keys:
            self._stack.setCurrentIndex(0)

    def select(self, key: str) -> None:
        """Show *key*'s pane + check its pill; emit ``pane_changed``. No-op if *key* is absent."""
        if key not in self._keys:
            return
        self._stack.setCurrentIndex(self._keys.index(key))
        self._pills.select(key)
        self.pane_changed.emit(key)

    def current(self) -> "Optional[str]":
        """The active pane key, or None if empty."""
        return self._pills.current()

    def keys(self) -> "list[str]":
        """The pane keys in order (skipped None panes are absent)."""
        return list(self._keys)
