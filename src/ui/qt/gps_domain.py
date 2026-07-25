"""GPS-Wardrive DOMAIN DETAIL — the GPS config of the shared three-panel :class:`DomainDetailView`.

A third domain on the same frame (after WiFi + BLE): the right panel is a :class:`GpsDetailPanel`
showing a selected :class:`~src.core.wardrive.WardriveObservation` — an access point seen at a GPS
location during a lawful, owner-authorized wardrive. Passive/awareness-only: it presents received
broadcast metadata plus the position it was heard at; it transmits nothing.
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
    "open": "Open",
    "weak": "Weak (WEP/WPS/WPA1)",
    "strong": "Strong (WPA2/WPA3)",
    "unknown": "Unknown",
}


class GpsDetailPanel(QFrame):
    """Right-panel detail for one wardrive observation (an AP heard at a GPS position)."""

    def __init__(self, parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self.setObjectName("gps_detail")
        self._obs = None
        v = QVBoxLayout(self)
        self._title = QLabel("No observation selected")
        self._title.setStyleSheet(f"color:{C.TEXT_PRIMARY}; font-weight:bold; font-size:12pt;")
        v.addWidget(self._title)
        form = QFormLayout()
        v.addLayout(form)
        v.addStretch(1)
        self._rows: dict[str, QLabel] = {}
        for key in ("BSSID", "Vendor", "Security", "Signal", "Channel", "Location", "Altitude",
                    "Fix"):
            val = QLabel("—")
            val.setStyleSheet(f"color:{C.TEXT_MUTED};")
            self._rows[key] = val
            form.addRow(QLabel(key), val)

    def set_object(self, obs) -> None:
        self._obs = obs
        self._title.setText(obs.display_name())
        self._rows["BSSID"].setText(obs.bssid or "—")
        self._rows["Vendor"].setText(lookup_vendor(obs.bssid) or "—")
        grade = security_grade(obs.auth)
        sec = self._rows["Security"]
        sec.setText(f"{obs.auth or '—'} · {_GRADE_LABEL[grade]}")
        sec.setStyleSheet(f"color:{_GRADE_COLOR.get(grade, C.TEXT_MUTED)};")
        # RSSI/channel 0 is the wardrive parser's unknown sentinel — show "—", not a fake 0.
        self._rows["Signal"].setText(f"{obs.rssi} dBm" if obs.rssi else "—")
        self._rows["Channel"].setText(str(obs.channel) if obs.channel else "—")
        self._rows["Location"].setText(f"{obs.lat:.6f}, {obs.lon:.6f}")
        self._rows["Altitude"].setText(f"{obs.alt:.1f} m")
        fix = self._rows["Fix"]
        fix.setText("GPS fix" if obs.has_fix else "No fix")
        fix.setStyleSheet(f"color:{C.SUCCESS if obs.has_fix else C.WARNING};")

    def clear(self) -> None:
        self._obs = None
        self._title.setText("No observation selected")
        for lbl in self._rows.values():
            lbl.setText("—")

    @property
    def observation(self):
        return self._obs


class GpsDomainView(DomainDetailView):
    """The GPS-Wardrive domain: the shared three-panel frame with a GPS :class:`GpsDetailPanel`."""

    def __init__(self, center: QWidget, detail: "Optional[GpsDetailPanel]" = None,
                 parent: "Optional[QWidget]" = None) -> None:
        super().__init__(center, detail if detail is not None else GpsDetailPanel(), parent)
