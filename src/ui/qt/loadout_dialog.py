"""Loadout picker — choose the firmwares + hardware you use, or 'Full Stack' (everything).

Shown once on first run (and re-openable from View ▸ Loadout) so the GUI can hide features you won't use.
Returns a loadout dict consumed by ``main_window.apply_loadout`` and persisted in settings. Pure-ish Qt
(no device access) so it's offscreen-testable via :meth:`build_result`.
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.config import loadout as L

# Friendly labels for the checklist (ids come from src/config/loadout.py).
_FW_LABELS = {
    "marauder": "ESP32 Marauder", "ghostesp": "GhostESP", "bruce": "Bruce",
    "halehound": "HaleHound", "esp32_div": "ESP32-DIV", "flipper": "Flipper Zero",
    "meshtastic": "Meshtastic", "bw16": "BW16 / RTL8720", "bluejammer": "BlueJammer (lab-only)",
}
_HW_LABELS = {
    "esp32": "ESP32 boards", "bw16": "BW16 / RTL8720DN", "flipper": "Flipper Zero",
    "raspberry_pi": "Raspberry Pi", "android_adb": "Android / ADB device",
    "gps": "GPS module (wardriving)", "usb_os": "PC / USB OS flashing (Kali/Tails/Arch)",
}

# One-line "what is this" descriptions, shown as checkbox tooltips.
_FW_DESC = {
    "marauder": "ESP32 Wi-Fi/Bluetooth pentest firmware — scans, deauth, and more.",
    "ghostesp": "Feature-rich ESP32 Wi-Fi/BLE firmware (Marauder alternative).",
    "bruce": "Multi-tool ESP32 firmware (Wi-Fi, BLE, IR, RF, RFID).",
    "halehound": "Wi-Fi hunting/tracking firmware for ESP32.",
    "esp32_div": "ESP32-DIV diagnostics/scanning firmware.",
    "flipper": "Flipper Zero multi-tool (sub-GHz, NFC, IR, GPIO).",
    "meshtastic": "Long-range LoRa mesh messaging firmware.",
    "bw16": "RTL8720 (BW16) dual-band Wi-Fi firmware.",
    "bluejammer": "Lab-only RF control firmware — legal in a shielded enclosure only.",
}
_HW_DESC = {
    "esp32": "ESP32-family dev boards (S2/S3/C3/C6, etc.).",
    "bw16": "BW16 / RTL8720DN dual-band Wi-Fi module.",
    "flipper": "Flipper Zero portable multi-tool.",
    "raspberry_pi": "Raspberry Pi single-board computer.",
    "android_adb": "Android phone/tablet controlled over ADB.",
    "gps": "GPS receiver for wardriving location tags.",
    "usb_os": "Write a bootable security OS (Kali/Tails/Arch) to a USB stick.",
}


class LoadoutDialog(QDialog):
    """Pick firmwares + hardware, or Full Stack. Result in :attr:`result_loadout` after exec_()."""

    def __init__(self, parent: Optional[QWidget] = None, current: "dict | None" = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose your loadout")
        self.setMinimumWidth(440)
        self.result_loadout: "dict | None" = None
        cur = L.normalize(current)
        self._fw_boxes: "dict[str, QCheckBox]" = {}
        self._hw_boxes: "dict[str, QCheckBox]" = {}

        root = QVBoxLayout(self)
        intro = QLabel(
            "Pick the <b>firmwares</b> and <b>hardware</b> you actually use — Cyber Controller hides the "
            "rest to stay uncluttered. You can change this anytime in <b>View ▸ Loadout</b>, or pick "
            "<b>Full Stack</b> for everything."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        body = QScrollArea()
        body.setWidgetResizable(True)
        body.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        cols = QHBoxLayout(inner)

        cols.addLayout(self._checklist("Firmwares", L.FIRMWARES, _FW_LABELS, self._fw_boxes,
                                       set(cur["firmwares"]) or {"marauder"}, _FW_DESC))
        cols.addLayout(self._checklist("Hardware", L.HARDWARE, _HW_LABELS, self._hw_boxes,
                                       set(cur["hardware"]) or {"esp32"}, _HW_DESC))
        body.setWidget(inner)
        root.addWidget(body, 1)

        btns = QHBoxLayout()
        full = QPushButton("Full Stack (everything)")
        full.setToolTip("Show every feature — the complete interface (you can trim it later).")
        full.clicked.connect(self._choose_full_stack)
        btns.addWidget(full)
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        apply_btn = QPushButton("Apply selection")
        apply_btn.setObjectName("flash_btn")  # primary accent style from the QSS
        apply_btn.clicked.connect(self._choose_selection)
        btns.addWidget(apply_btn)
        root.addLayout(btns)

    def _checklist(self, title, ids, labels, store, checked, descriptions=None):
        lay = QVBoxLayout()
        head = QLabel(title)
        head.setObjectName("card_title")
        lay.addWidget(head)
        descriptions = descriptions or {}
        for i in ids:
            cb = QCheckBox(labels.get(i, i))
            cb.setChecked(i in checked)
            if descriptions.get(i):
                cb.setToolTip(descriptions[i])
            store[i] = cb
            lay.addWidget(cb)
        lay.addStretch(1)
        return lay

    def build_result(self, full_stack: bool) -> dict:
        """Compute the loadout dict from the current checkbox state (testable without exec_)."""
        if full_stack:
            return L.full_stack_loadout()
        return {
            "full_stack": False,
            "configured": True,
            "firmwares": [i for i, cb in self._fw_boxes.items() if cb.isChecked()],
            "hardware": [i for i, cb in self._hw_boxes.items() if cb.isChecked()],
        }

    def _choose_full_stack(self) -> None:
        self.result_loadout = self.build_result(full_stack=True)
        self.accept()

    def _choose_selection(self) -> None:
        self.result_loadout = self.build_result(full_stack=False)
        self.accept()

    @staticmethod
    def choose(parent: Optional[QWidget], current: "dict | None") -> "dict | None":
        dlg = LoadoutDialog(parent, current)
        return dlg.result_loadout if dlg.exec_() == QDialog.Accepted else None
