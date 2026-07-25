"""Wi-Fi DOMAIN DETAIL — the three-panel master/detail screen (design brief).

LEFT: the posture panel — a hard PASSIVE (Sniff/Scan, the default) vs ACTIVE (Attack) split. Active
is a real boundary, not a peer tab: switching to it requires an explicit authorization step
(``active_authorization_requested`` — the host runs the safety confirm), never the same tap as a
passive read. CENTER: the live object table (composed/injected). RIGHT: the contextual
:class:`APDetailPanel` for the selected access point.

Responsive: three panels side-by-side on a roomy canvas; the right detail collapses (hidden until an
object is selected) when the canvas is cramped, via the pure ``layout_profile().columns`` resolver.
Presentation only — behaviour (scan start, the authorized attack) is wired by the host; this widget
ENFORCES the passive/active boundary and never weakens the safety consent system.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QButtonGroup,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.core.oui import lookup_vendor
from src.core.wifi_analyzer import security_grade
from src.ui.qt.layout_profile import layout_profile
from src.ui.qt.theme import colors as C

_GRADE_COLOR = {"open": C.ERROR, "weak": C.WARNING, "strong": C.SUCCESS}
_GRADE_LABEL = {
    "open": "Open — no encryption",
    "weak": "Weak — WEP / WPS / WPA1",
    "strong": "Strong — WPA2 / WPA3",
    "unknown": "Unknown",
}


class APDetailPanel(QFrame):
    """Right-panel detail for one access point: its identity + an honest security read. ``set_ap``
    fills it; ``clear`` returns it to the empty state."""

    def __init__(self, parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self.setObjectName("ap_detail")
        self._ap = None
        v = QVBoxLayout(self)
        self._title = QLabel("No access point selected")
        self._title.setStyleSheet(f"color:{C.TEXT_PRIMARY}; font-weight:bold; font-size:12pt;")
        v.addWidget(self._title)
        form = QFormLayout()
        v.addLayout(form)
        v.addStretch(1)
        self._rows: dict[str, QLabel] = {}
        for key in ("BSSID", "Vendor", "Security", "Signal", "Channel", "Clients"):
            val = QLabel("—")
            val.setStyleSheet(f"color:{C.TEXT_MUTED};")
            self._rows[key] = val
            form.addRow(QLabel(key), val)

    def set_ap(self, ap) -> None:
        self._ap = ap
        self._title.setText(ap.display_ssid())
        self._rows["BSSID"].setText(ap.bssid or "—")
        self._rows["Vendor"].setText(lookup_vendor(ap.bssid) or "—")
        grade = security_grade(ap.encryption)
        sec = self._rows["Security"]
        sec.setText(f"{ap.enc_label()} · {_GRADE_LABEL[grade]}")
        sec.setStyleSheet(f"color:{_GRADE_COLOR.get(grade, C.TEXT_MUTED)};")
        self._rows["Signal"].setText("—" if ap.rssi is None else f"{ap.rssi} dBm")
        self._rows["Channel"].setText("—" if ap.channel is None else str(ap.channel))
        self._rows["Clients"].setText(str(ap.client_count()))

    def clear(self) -> None:
        self._ap = None
        self._title.setText("No access point selected")
        for lbl in self._rows.values():
            lbl.setText("—")

    @property
    def ap(self):
        return self._ap


class WifiDomainView(QWidget):
    """Three-panel master/detail: posture (left) · object table (center) · detail (right)."""

    POSTURE_PASSIVE = "passive"
    POSTURE_ACTIVE = "active"

    active_authorization_requested = pyqtSignal()
    object_selected = pyqtSignal(object)

    def __init__(self, center: QWidget, detail: "Optional[APDetailPanel]" = None,
                 parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._posture = self.POSTURE_PASSIVE
        self._selected = None
        self._cols = 0
        self._center = center
        self._detail = detail if detail is not None else APDetailPanel()
        self._left = self._build_posture_panel()

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(self._left, 0)
        row.addWidget(self._center, 3)
        row.addWidget(self._detail, 2)
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
    def select_object(self, ap) -> None:
        self._selected = ap
        self._detail.set_ap(ap)
        self.object_selected.emit(ap)
        self._apply_layout()  # a selection reveals the detail on a cramped canvas

    def clear_selection(self) -> None:
        self._selected = None
        self._detail.clear()
        self._apply_layout()

    @property
    def detail(self) -> APDetailPanel:
        return self._detail

    # ── responsive ──
    def columns(self) -> int:
        return self._cols

    def detail_visible(self) -> bool:
        """The right detail is shown on a roomy (expanded) canvas, or whenever an object is picked;
        on a cramped canvas with nothing selected it collapses."""
        return self._cols >= 3 or self._selected is not None

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._relayout_for_size()

    def _relayout_for_size(self) -> None:
        self._cols = layout_profile(max(1, self.width()), max(1, self.height())).columns
        self._apply_layout()

    def _apply_layout(self) -> None:
        self._detail.setVisible(self.detail_visible())
