"""Wi-Fi DOMAIN DETAIL — the WiFi configuration of the shared three-panel :class:`DomainDetailView`.

The three-panel frame (posture boundary · object table · detail · responsive reflow) lives in
``domain_view``; this module is the WiFi specialization — its right panel is an
:class:`APDetailPanel` (a selected AP's identity + an honest security read: OUI vendor + grade).
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtWidgets import QFormLayout, QFrame, QLabel, QVBoxLayout, QWidget

from src.core.oui import lookup_vendor
from src.core.wifi_analyzer import security_grade
from src.ui.qt.domain_view import DomainDetailView
from src.ui.qt.theme import colors as C

_GRADE_COLOR = {"open": C.ERROR, "weak": C.WARNING, "strong": C.SUCCESS}
_GRADE_LABEL = {
    "open": "Open — no encryption",
    "weak": "Weak — WEP / WPS / WPA1",
    "strong": "Strong — WPA2 / WPA3",
    "unknown": "Unknown",
}


class APDetailPanel(QFrame):
    """Right-panel detail for one access point: identity + an honest security read. ``set_object``
    fills it (``set_ap`` is a back-compat alias); ``clear`` returns it to the empty state."""

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

    def set_object(self, ap) -> None:
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

    set_ap = set_object  # back-compat alias

    def clear(self) -> None:
        self._ap = None
        self._title.setText("No access point selected")
        for lbl in self._rows.values():
            lbl.setText("—")

    @property
    def ap(self):
        return self._ap


class WifiDomainView(DomainDetailView):
    """The WiFi domain: the shared three-panel frame with a WiFi :class:`APDetailPanel`."""

    def __init__(self, center: QWidget, detail: "Optional[APDetailPanel]" = None,
                 parent: "Optional[QWidget]" = None) -> None:
        super().__init__(center, detail if detail is not None else APDetailPanel(), parent)
