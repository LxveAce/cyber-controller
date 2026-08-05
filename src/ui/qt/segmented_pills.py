"""SegmentedPills — the bounded second-axis sub-nav for the reform IA (W1).

A segmented row of at most four mutually-exclusive pills (DEVICE's Dashboard/Firmware/OS/Mesh,
OPERATE's Console/Macros, …). It replaces the inner ``QTabWidget`` strips whose overflow into
scroll-arrows is the exact defect the reform kills — this never scrolls and never overflows: the
IA guarantees <= 4 segments, and the row stays fully visible. Purple ``ACCENT`` marks the active
pill; green is reserved for live state, so it is never used here.

Pure sub-nav: it emits the chosen key and paints selection. It owns no screen and no send path.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QButtonGroup, QHBoxLayout, QPushButton, QWidget

from src.ui.qt.theme import colors as C

_MAX = 4   # the reform's bounded second axis: at most four pills, never a scrolling tab strip


class SegmentedPills(QWidget):
    """A <=4 single-select pill row. ``segment_selected`` fires only on a user click, not on
    programmatic :meth:`select`, so a host reflects an external nav change with no feedback loop."""

    segment_selected = pyqtSignal(str)

    def __init__(self, parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._pills: "list[tuple[str, QPushButton]]" = []
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(4)
        self._row.addStretch(1)   # left-align the pills; the stretch stays at the end

    def set_segments(self, segments: "list[tuple[str, str]]") -> None:
        """(Re)build the pills from ``(key, label)`` pairs. Bounded to the first four (the IA never
        passes more). The first pill is selected without emitting."""
        for _key, btn in self._pills:
            self._group.removeButton(btn)
            btn.setParent(None)
            btn.deleteLater()
        self._pills = []
        for key, label in list(segments)[:_MAX]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setMinimumHeight(28)
            btn.setStyleSheet(self._pill_qss())
            btn.clicked.connect(lambda _checked=False, k=key: self.segment_selected.emit(k))
            self._group.addButton(btn)
            self._pills.append((key, btn))
            self._row.insertWidget(self._row.count() - 1, btn)   # before the trailing stretch
        if self._pills:
            self._pills[0][1].setChecked(True)

    @staticmethod
    def _pill_qss() -> str:
        return (
            f"QPushButton{{color:{C.TEXT_MUTED}; background:{C.BG_CARD};"
            f" border:1px solid {C.BORDER}; border-radius:13px; padding:3px 14px;}}"
            f"QPushButton:hover{{color:{C.TEXT_PRIMARY};}}"
            f"QPushButton:checked{{color:{C.TEXT_PRIMARY}; background:{C.ACCENT_DIM};"
            f" border:1px solid {C.ACCENT};}}")

    def select(self, key: str) -> None:
        """Check *key*'s pill (unchecking the rest) WITHOUT emitting — for reflecting an external
        nav change. A no-op if *key* isn't a segment."""
        for k, btn in self._pills:
            if k == key:
                btn.setChecked(True)
                return

    def current(self) -> "Optional[str]":
        """The selected segment key, or None if empty."""
        for k, btn in self._pills:
            if btn.isChecked():
                return k
        return None

    def keys(self) -> "list[str]":
        """The segment keys in order."""
        return [k for k, _ in self._pills]
