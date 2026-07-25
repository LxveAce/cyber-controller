"""BLE DOMAIN DETAIL — the BLE configuration of the shared three-panel :class:`DomainDetailView`.

Proves the dual-axis frame is general, not a WiFi one-off: BLE reuses the exact same frame (posture
boundary · object table · detail · responsive reflow) as :class:`WifiDomainView`, differing only in
the right panel — a :class:`BleDetailPanel` showing a selected BLE device's identity, its resolved
vendor + GAP-appearance class, and an honest tracker flag (a defense-aligned read).
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtWidgets import QFormLayout, QFrame, QLabel, QVBoxLayout, QWidget

from src.ui.qt.domain_view import DomainDetailView
from src.ui.qt.theme import colors as C


class BleDetailPanel(QFrame):
    """Right-panel detail for one BLE device. ``set_object`` fills it; ``clear`` empties it."""

    def __init__(self, parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self.setObjectName("ble_detail")
        self._dev = None
        v = QVBoxLayout(self)
        self._title = QLabel("No device selected")
        self._title.setStyleSheet(f"color:{C.TEXT_PRIMARY}; font-weight:bold; font-size:12pt;")
        v.addWidget(self._title)
        form = QFormLayout()
        v.addLayout(form)
        v.addStretch(1)
        self._rows: dict[str, QLabel] = {}
        for key in ("Address", "Addr type", "Vendor", "Appearance", "Signal", "Tracker"):
            val = QLabel("—")
            val.setStyleSheet(f"color:{C.TEXT_MUTED};")
            self._rows[key] = val
            form.addRow(QLabel(key), val)

    def set_object(self, dev) -> None:
        self._dev = dev
        self._title.setText(dev.display_name())
        self._rows["Address"].setText(dev.addr or "—")
        self._rows["Addr type"].setText(dev.addr_type or "—")
        self._rows["Vendor"].setText(dev.company_name or dev.vendor or "—")
        self._rows["Appearance"].setText(dev.appearance_name or "—")
        self._rows["Signal"].setText("—" if dev.rssi is None else f"{dev.rssi} dBm")
        tracker = self._rows["Tracker"]
        tracker.setText("Yes — possible tracker" if dev.tracker else "No")
        tracker.setStyleSheet(f"color:{C.WARNING if dev.tracker else C.TEXT_MUTED};")

    def clear(self) -> None:
        self._dev = None
        self._title.setText("No device selected")
        for lbl in self._rows.values():
            lbl.setText("—")

    @property
    def device(self):
        return self._dev


class BleDomainView(DomainDetailView):
    """The BLE domain: the shared three-panel frame with a BLE :class:`BleDetailPanel`."""

    def __init__(self, center: QWidget, detail: "Optional[BleDetailPanel]" = None,
                 parent: "Optional[QWidget]" = None) -> None:
        super().__init__(center, detail if detail is not None else BleDetailPanel(), parent)
