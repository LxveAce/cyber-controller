"""Settings tab — edit persistent application configuration.

Backed by :mod:`src.config.settings`.  Groups settings into Serial, Flash,
Cross-Comm, and Firmware Vault sections.  Save writes to disk; Reset restores
the in-memory defaults (and the user can then Save to persist them).
"""

from __future__ import annotations

import logging

from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import DEFAULTS, load_settings, save_settings

log = logging.getLogger(__name__)


def _make_card(title: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    """Create a card-styled QFrame with optional title label."""
    card = QFrame()
    card.setObjectName("card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(8)
    if title:
        lbl = QLabel(title)
        lbl.setObjectName("card_title")
        layout.addWidget(lbl)
    return card, layout


class SettingsTab(QWidget):
    """Editor for persistent application settings.

    Reads via :func:`load_settings`, writes via :func:`save_settings`.
    Reloads from disk each time the tab is shown so it never displays stale
    values after another component changed the file.
    """

    def __init__(self) -> None:
        super().__init__()
        self._settings = load_settings()
        self._build_ui()
        self._connect_signals()
        self._load_into_ui(self._settings)

    # ── Layout ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        root = QVBoxLayout(container)

        # ── Serial ───────────────────────────────────────────────────
        serial_card, serial_outer = _make_card("Serial Defaults")
        serial_form = QFormLayout()
        serial_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self._baud_combo = QComboBox()
        self._baud_combo.setEditable(True)
        self._baud_combo.setMinimumWidth(120)
        self._baud_combo.addItems(["9600", "57600", "115200", "230400", "460800", "921600"])
        self._timeout_spin = QSpinBox()
        self._timeout_spin.setRange(1, 120)
        self._timeout_spin.setSuffix(" s")
        self._timeout_spin.setMinimumWidth(80)
        serial_form.addRow("Default Baud Rate:", self._baud_combo)
        serial_form.addRow("Connection Timeout:", self._timeout_spin)
        serial_outer.addLayout(serial_form)
        root.addWidget(serial_card)

        # ── Flash ────────────────────────────────────────────────────
        flash_card, flash_outer = _make_card("Flash Defaults")
        flash_form = QFormLayout()
        flash_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self._flash_baud_combo = QComboBox()
        self._flash_baud_combo.setEditable(True)
        self._flash_baud_combo.setMinimumWidth(120)
        self._flash_baud_combo.addItems(["115200", "230400", "460800", "921600"])
        self._flash_mode_combo = QComboBox()
        self._flash_mode_combo.setMinimumWidth(100)
        self._flash_mode_combo.addItems(["qio", "qout", "dio", "dout"])
        self._verify_check = QCheckBox("Verify after flash")
        self._backup_check = QCheckBox("Auto-backup before flash")
        flash_form.addRow("Flash Baud Rate:", self._flash_baud_combo)
        flash_form.addRow("Flash Mode:", self._flash_mode_combo)
        flash_form.addRow(self._verify_check)
        flash_form.addRow(self._backup_check)
        flash_outer.addLayout(flash_form)
        root.addWidget(flash_card)

        # ── Cross-Comm ───────────────────────────────────────────────
        comm_card, comm_outer = _make_card("Cross-Communication")
        comm_form = QFormLayout()
        comm_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self._auto_share_check = QCheckBox("Auto-share discoveries to the shared target pool")
        self._dedup_check = QCheckBox("De-duplicate targets by MAC")
        comm_form.addRow(self._auto_share_check)
        comm_form.addRow(self._dedup_check)
        comm_outer.addLayout(comm_form)
        root.addWidget(comm_card)

        # ── Safety & Disclaimers ─────────────────────────────────────
        # These LABEL and warn; they never remove or block a capability. The
        # confirm dialog always offers "Yes, proceed"; suppress turns it off.
        safety_card, safety_outer = _make_card("Safety & Disclaimers")
        safety_form = QFormLayout()
        safety_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self._confirm_dangerous_check = QCheckBox(
            "Confirm before sending dangerous commands (deauth / jam / beacon spam / ...)"
        )
        self._suppress_warnings_check = QCheckBox(
            "Suppress all safety warnings (controlled-lab use — you remain responsible)"
        )
        safety_form.addRow(self._confirm_dangerous_check)
        safety_form.addRow(self._suppress_warnings_check)
        safety_outer.addLayout(safety_form)
        root.addWidget(safety_card)

        # ── Firmware Vault ───────────────────────────────────────────
        vault_card, vault_outer = _make_card("Firmware Vault")
        vault_form = QFormLayout()
        vault_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        dir_row = QHBoxLayout()
        self._vault_dir_edit = QLineEdit()
        self._vault_dir_edit.setPlaceholderText("~/.cyber-controller/firmware")
        self._vault_dir_edit.setMinimumWidth(150)
        self._vault_browse_btn = QPushButton("Browse...")
        dir_row.addWidget(self._vault_dir_edit, stretch=1)
        dir_row.addWidget(self._vault_browse_btn)
        vault_form.addRow("Vault Directory:", dir_row)
        vault_outer.addLayout(vault_form)
        root.addWidget(vault_card)

        # ── Save / Reset ─────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._reset_btn = QPushButton("Reset to Defaults")
        self._save_btn = QPushButton("Save Settings")
        self._save_btn.setObjectName("flash_btn")
        btn_row.addWidget(self._reset_btn)
        btn_row.addWidget(self._save_btn)
        root.addLayout(btn_row)

        root.addStretch()

        scroll.setWidget(container)
        outer.addWidget(scroll)

    def _connect_signals(self) -> None:
        self._save_btn.clicked.connect(self._on_save)
        self._reset_btn.clicked.connect(self._on_reset)
        self._vault_browse_btn.clicked.connect(self._on_browse_vault)
        self._suppress_warnings_check.toggled.connect(self._on_suppress_toggled)

    # ── Load / gather ────────────────────────────────────────────────

    def _load_into_ui(self, settings: dict) -> None:
        """Populate widgets from a settings dict."""
        serial = settings.get("serial", {})
        self._set_combo_text(self._baud_combo, str(serial.get("default_baud", 115200)))
        self._timeout_spin.setValue(int(serial.get("timeout", 5)))

        flash = settings.get("flash", {})
        self._set_combo_text(self._flash_baud_combo, str(flash.get("flash_baud", 921600)))
        mode = str(flash.get("mode", "dio"))
        idx = self._flash_mode_combo.findText(mode)
        if idx >= 0:
            self._flash_mode_combo.setCurrentIndex(idx)
        self._verify_check.setChecked(bool(flash.get("verify", True)))
        self._backup_check.setChecked(bool(flash.get("auto_backup", True)))

        comm = settings.get("cross_comm", {})
        self._auto_share_check.setChecked(bool(comm.get("auto_share", True)))
        self._dedup_check.setChecked(bool(comm.get("dedup_by_mac", True)))

        sec = settings.get("safety", {})
        self._confirm_dangerous_check.setChecked(bool(sec.get("confirm_dangerous", True)))
        # Set the suppress box WITHOUT triggering its acknowledgement dialog.
        self._suppress_warnings_check.blockSignals(True)
        self._suppress_warnings_check.setChecked(bool(sec.get("suppress_all_warnings", False)))
        self._suppress_warnings_check.blockSignals(False)

        vault = settings.get("vault", {})
        self._vault_dir_edit.setText(str(vault.get("dir", "")))

    def _gather(self) -> dict:
        """Read the current UI state into a settings dict."""
        return {
            "serial": {
                "default_baud": self._parse_int(self._baud_combo.currentText(), 115200),
                "timeout": self._timeout_spin.value(),
            },
            "flash": {
                "flash_baud": self._parse_int(self._flash_baud_combo.currentText(), 921600),
                "verify": self._verify_check.isChecked(),
                "auto_backup": self._backup_check.isChecked(),
                "mode": self._flash_mode_combo.currentText(),
            },
            "cross_comm": {
                "auto_share": self._auto_share_check.isChecked(),
                "dedup_by_mac": self._dedup_check.isChecked(),
            },
            "vault": {
                "dir": self._vault_dir_edit.text().strip(),
            },
            "safety": {
                "confirm_dangerous": self._confirm_dangerous_check.isChecked(),
                "suppress_all_warnings": self._suppress_warnings_check.isChecked(),
            },
            # Preserve the one-time disclaimer ack: _gather rebuilds the whole dict,
            # so without carrying it forward a Save would reset it to False and
            # re-show the first-run disclaimer on next launch.
            "_disclaimer_ack": self._settings.get("_disclaimer_ack", False),
        }

    # ── Actions ──────────────────────────────────────────────────────

    def _on_save(self) -> None:
        self._settings = self._gather()
        try:
            save_settings(self._settings)
            QMessageBox.information(self, "Settings", "Settings saved successfully.")
        except Exception as exc:  # noqa: BLE001 — surface any I/O error to the user
            log.exception("Failed to save settings")
            QMessageBox.critical(self, "Error", f"Failed to save settings:\n{exc}")

    def _on_reset(self) -> None:
        reply = QMessageBox.question(
            self,
            "Reset Settings",
            "Reset all fields to defaults? (You must Save to persist.)",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            # DEFAULTS now contains a top-level scalar (_disclaimer_ack), so guard
            # the dict() copy against non-dict values.
            self._settings = {
                k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()
            }
            self._load_into_ui(self._settings)

    def _on_suppress_toggled(self, checked: bool) -> None:
        """One-time acknowledgement when ENABLING 'suppress all warnings'.

        Enabling it removes every per-command safety confirmation, so we make the
        user acknowledge once; cancelling re-unchecks the box.
        """
        if not checked:
            return
        reply = QMessageBox.warning(
            self,
            "Suppress all safety warnings",
            "This disables every per-command safety confirmation. Dangerous commands "
            "(deauth, jamming, beacon spam) will be sent with no prompt.\n\n"
            "You remain solely responsible for lawful, authorized use. Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            self._suppress_warnings_check.blockSignals(True)
            self._suppress_warnings_check.setChecked(False)
            self._suppress_warnings_check.blockSignals(False)

    def _on_browse_vault(self) -> None:
        start = self._vault_dir_edit.text().strip() or ""
        path = QFileDialog.getExistingDirectory(self, "Select Firmware Vault Directory", start)
        if path:
            self._vault_dir_edit.setText(path)

    # ── Qt overrides ─────────────────────────────────────────────────

    def showEvent(self, event) -> None:  # noqa: N802 — Qt naming
        """Reload settings from disk whenever the tab becomes visible."""
        super().showEvent(event)
        self._settings = load_settings()
        self._load_into_ui(self._settings)

    # ── Accessors / helpers ──────────────────────────────────────────

    def get_settings(self) -> dict:
        """Return the most recently saved/loaded settings dict."""
        return self._settings

    @staticmethod
    def _set_combo_text(combo: QComboBox, text: str) -> None:
        idx = combo.findText(text)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        else:
            combo.setEditText(text)

    @staticmethod
    def _parse_int(text: str, fallback: int) -> int:
        try:
            return int(str(text).strip())
        except (TypeError, ValueError):
            return fallback
