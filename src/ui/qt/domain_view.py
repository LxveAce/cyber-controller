"""Generic DOMAIN DETAIL frame (design brief) — the shared three-panel master/detail used by every
radio domain, so the dual-axis frame is general, not a per-domain one-off.

LEFT: the posture panel — a hard PASSIVE (Sniff/Scan, the default) vs ACTIVE (Attack) split. Active
is a real boundary: switching to it emits ``active_authorization_requested`` (the host runs the
safety confirm) and posture flips only on ``confirm_active`` — never the same tap as a passive read.
CENTER: the live object table (injected). RIGHT: a detail widget for the selected object (injected;
must implement ``set_object(obj)`` + ``clear()``).

Responsive: three panels side-by-side on a roomy canvas, stacked vertically on a compact one; the
right detail collapses (hidden until an object is selected) when cramped — all from the pure
``layout_profile().columns`` resolver. Presentation only — the host wires behaviour; this frame
ENFORCES the passive/active boundary and never weakens the safety consent system.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QBoxLayout,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.ui.qt.layout_profile import layout_profile
from src.ui.qt.theme import colors as C


class DomainDetailView(QWidget):
    """Three-panel master/detail: posture (left) · object table (center) · detail (right)."""

    POSTURE_PASSIVE = "passive"
    POSTURE_ACTIVE = "active"

    active_authorization_requested = pyqtSignal()
    object_selected = pyqtSignal(object)

    def __init__(self, center: QWidget, detail: QWidget,
                 parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._posture = self.POSTURE_PASSIVE
        self._selected = None
        self._cols = 0
        self._center = center
        self._detail = detail
        self._left = self._build_posture_panel()

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(self._left, 0)
        row.addWidget(self._center, 3)
        row.addWidget(self._detail, 2)
        self._row = row
        self._relayout_for_size()

    def _build_posture_panel(self) -> QWidget:
        panel = QFrame()
        v = QVBoxLayout(panel)
        title = QLabel("Posture")
        title.setStyleSheet(f"color:{C.TEXT_MUTED}; font-size:9pt;")
        v.addWidget(title)
        self._btn_passive = QPushButton("Passive · Scan")
        self._btn_active = QPushButton("Active · Attack")
        for b in (self._btn_passive, self._btn_active):
            b.setCheckable(True)
        self._btn_passive.setChecked(True)
        group = QButtonGroup(panel)
        group.setExclusive(True)
        group.addButton(self._btn_passive)
        group.addButton(self._btn_active)
        self._btn_passive.clicked.connect(self.set_passive)
        # Active is a boundary: clicking it does NOT flip posture — it requests authorization.
        self._btn_active.clicked.connect(self.request_active)
        v.addWidget(self._btn_passive)
        v.addWidget(self._btn_active)
        note = QLabel("Active tools require authorization.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{C.TEXT_MUTED}; font-size:8pt;")
        v.addWidget(note)
        v.addStretch(1)
        return panel

    # ── posture boundary ──
    def posture(self) -> str:
        return self._posture

    def request_active(self) -> None:
        """Ask to enter ACTIVE. Does NOT flip posture — emits the authorization request so the host
        runs the safety confirm. The boundary: entering Active is never one tap."""
        if self._posture != self.POSTURE_ACTIVE:
            self._btn_passive.setChecked(True)
            self._btn_active.setChecked(False)
            self.active_authorization_requested.emit()

    def confirm_active(self) -> None:
        """Enter ACTIVE — called by the host ONLY after the safety authorization succeeds."""
        self._posture = self.POSTURE_ACTIVE
        self._btn_active.setChecked(True)
        self._btn_passive.setChecked(False)

    def set_passive(self) -> None:
        self._posture = self.POSTURE_PASSIVE
        self._btn_passive.setChecked(True)
        self._btn_active.setChecked(False)

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
