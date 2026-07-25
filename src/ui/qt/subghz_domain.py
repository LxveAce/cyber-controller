"""RF / Sub-GHz DOMAIN DETAIL — the SubGHz config of the shared :class:`DomainDetailView` frame.

A fourth domain on the same frame (after WiFi + BLE + GPS): the right panel is a
:class:`SubGhzDetailPanel` showing a selected :class:`~src.core.subghz_ook.SubGhzSignal` — a decoded
OOK remote/sensor capture (protocol, code, address, data, bits + optional RSSI/frequency).
RX/awareness-only: it presents received/decoded signal data; it never transmits an OOK frame.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtWidgets import QFormLayout, QFrame, QLabel, QVBoxLayout, QWidget

from src.ui.qt.domain_view import DomainDetailView
from src.ui.qt.theme import colors as C


class SubGhzDetailPanel(QFrame):
    """Detail panel for one decoded Sub-GHz signal. ``set_object`` fills; ``clear`` empties."""

    def __init__(self, parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self.setObjectName("subghz_detail")
        self._sig = None
        v = QVBoxLayout(self)
        self._title = QLabel("No signal selected")
        self._title.setStyleSheet(f"color:{C.TEXT_PRIMARY}; font-weight:bold; font-size:12pt;")
        v.addWidget(self._title)
        form = QFormLayout()
        v.addLayout(form)
        v.addStretch(1)
        self._rows: dict[str, QLabel] = {}
        for key in ("Protocol", "Code", "Address", "Data", "Bits", "Signal", "Frequency"):
            val = QLabel("—")
            val.setStyleSheet(f"color:{C.TEXT_MUTED};")
            self._rows[key] = val
            form.addRow(QLabel(key), val)

    def set_object(self, sig) -> None:
        self._sig = sig
        self._title.setText((sig.protocol or "signal").upper())
        self._rows["Protocol"].setText(sig.protocol or "—")
        self._rows["Code"].setText(sig.code_hex())
        self._rows["Address"].setText(sig.address_hex())
        self._rows["Data"].setText(sig.data_hex())
        self._rows["Bits"].setText(str(sig.bits))
        self._rows["Signal"].setText("—" if sig.rssi is None else f"{sig.rssi} dBm")
        self._rows["Frequency"].setText(
            "—" if sig.frequency_mhz is None else f"{sig.frequency_mhz:.2f} MHz")

    def clear(self) -> None:
        self._sig = None
        self._title.setText("No signal selected")
        for lbl in self._rows.values():
            lbl.setText("—")

    @property
    def signal(self):
        return self._sig


class SubGhzDomainView(DomainDetailView):
    """The RF/Sub-GHz domain: the shared three-panel frame with a :class:`SubGhzDetailPanel`."""

    def __init__(self, center: QWidget, detail: "Optional[SubGhzDetailPanel]" = None,
                 parent: "Optional[QWidget]" = None) -> None:
        super().__init__(center, detail if detail is not None else SubGhzDetailPanel(), parent)
