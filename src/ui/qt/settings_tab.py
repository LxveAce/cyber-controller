"""Settings tab — edit persistent application configuration.

Backed by :mod:`src.config.settings`.  Groups settings into Serial, Flash,
Cross-Comm, and Firmware Vault sections.  Save writes to disk; Reset restores
the in-memory defaults (and the user can then Save to persist them).
"""

from __future__ import annotations

import logging

from PyQt5.QtCore import pyqtSignal
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

    #: Emitted when the user clicks "Check now" — the main window runs a manual (forced) update check.
    check_updates_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._settings = load_settings()
        self._build_ui()
        self._connect_signals()
        self._load_into_ui(self._settings)
        self._refresh_gate_status()

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
        serial_form.addRow("Default Baud Rate:", self._baud_combo)
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
        flash_form.addRow("Flash Baud Rate:", self._flash_baud_combo)
        flash_outer.addLayout(flash_form)
        root.addWidget(flash_card)

        # ── Cross-Comm ───────────────────────────────────────────────
        # The old "Auto-share discoveries" / "De-duplicate targets by MAC" checkboxes were inert — nothing
        # read them, so unchecking either changed nothing. Rather than ship toggles that lie, describe the
        # real, always-on behavior honestly (removal mirrors how the inert serial.timeout / flash.* controls
        # were handled). De-dup by MAC is intrinsic to the pool: targets are keyed by type:mac.
        self._comm_card, comm_outer = _make_card("Cross-Communication")
        comm_card = self._comm_card
        comm_desc = QLabel(
            "Every device's scan discoveries are shared into one target pool and de-duplicated by MAC, so "
            "an AP found by one radio is actionable from another. This is always on — there is nothing to "
            "configure here."
        )
        comm_desc.setObjectName("muted")
        comm_desc.setWordWrap(True)
        comm_outer.addWidget(comm_desc)
        root.addWidget(comm_card)

        # ── Updates ──────────────────────────────────────────────────
        self._updates_card, updates_outer = _make_card("Updates")
        updates_card = self._updates_card
        updates_desc = QLabel(
            "On launch, quietly check GitHub for a newer release and offer a link to download it. "
            "No auto-download or self-update — you stay in control of what gets installed."
        )
        updates_desc.setObjectName("muted")
        updates_desc.setWordWrap(True)
        updates_outer.addWidget(updates_desc)
        self._updates_enabled_check = QCheckBox("Automatically check for updates")
        updates_outer.addWidget(self._updates_enabled_check)
        updates_btn_row = QHBoxLayout()
        updates_btn_row.addStretch()
        self._check_updates_btn = QPushButton("Check now")
        updates_btn_row.addWidget(self._check_updates_btn)
        updates_outer.addLayout(updates_btn_row)
        root.addWidget(updates_card)

        # ── Safety & Disclaimers ─────────────────────────────────────
        # These LABEL and warn; they never remove or block a capability. The
        # confirm dialog always offers "Yes, proceed"; suppress turns it off.
        self._safety_card, safety_outer = _make_card("Safety & Disclaimers")
        safety_card = self._safety_card
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

        # ── Access Gate (Security) ───────────────────────────────────
        self._gate_card, gate_outer = _make_card("Access Gate (Security)")
        gate_card = self._gate_card
        gate_desc = QLabel(
            "Lock the app behind an admin password and/or a physical USB key. Secrets are stored as "
            "salted hashes (no plaintext); protected data stays encrypted until unlocked. Applies on "
            "the next launch."
        )
        gate_desc.setObjectName("muted")
        gate_desc.setWordWrap(True)
        gate_outer.addWidget(gate_desc)
        self._gate_status_lbl = QLabel("")
        self._gate_status_lbl.setObjectName("muted")
        self._gate_status_lbl.setWordWrap(True)
        gate_outer.addWidget(self._gate_status_lbl)
        self._gate_setup_btn = QPushButton("Set up access gate (password / key)…")
        self._gate_setup_btn.setToolTip("Set or change the admin password, create a physical USB key, "
                                        "or choose the unlock policy — from inside the app.")
        gate_outer.addWidget(self._gate_setup_btn)
        root.addWidget(gate_card)

        # ── Secure Container (Security) ──────────────────────────────
        # When ON, app-internal saves (logs/sessions/captures) are encrypted at rest in a gate-keyed
        # container and are unreadable while the access gate is locked. Off by default.
        self._secure_card, secure_outer = _make_card("Secure Container (Security)")
        secure_card = self._secure_card
        secure_desc = QLabel(
            "Encrypt app-saved data (logs, sessions, captures) at rest in a gate-keyed container "
            "(~/.cyber-controller/secure). The container is sealed — unreadable — whenever the access "
            "gate is locked, so logs can't be recovered off-disk without unlocking. The encryption key "
            "lives only inside the unlocked vault (never in the clear). Explicit exports you choose to "
            "share (e.g. a WiGLE wardrive CSV) stay plaintext by design."
        )
        secure_desc.setObjectName("muted")
        secure_desc.setWordWrap(True)
        secure_outer.addWidget(secure_desc)
        self._secure_container_check = QCheckBox("Store app logs/sessions/captures in the secure container")
        secure_outer.addWidget(self._secure_container_check)
        root.addWidget(secure_card)

        # ── Firmware Vault ───────────────────────────────────────────
        self._vault_card, vault_outer = _make_card("Firmware Vault")
        vault_card = self._vault_card
        vault_form = QFormLayout()
        vault_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        dir_row = QHBoxLayout()
        self._vault_dir_edit = QLineEdit()
        self._vault_dir_edit.setPlaceholderText("~/.cyber-controller/firmware_vault")
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

    # ── Dual-depth (Simple / Pro) ────────────────────────────────────

    def set_ui_mode(self, mode: str) -> None:
        """Simple shows only Serial + Flash defaults; Pro shows every section. Advanced sections
        (Cross-Comm, Safety & Disclaimers, Access Gate, Secure Container, Firmware Vault) are hidden
        in Simple — their saved values are untouched, just not editable in the streamlined view."""
        pro = str(mode).lower() != "simple"
        for card in (
            getattr(self, "_comm_card", None), getattr(self, "_safety_card", None),
            getattr(self, "_gate_card", None), getattr(self, "_secure_card", None),
            getattr(self, "_vault_card", None),
        ):
            if card is not None:
                card.setVisible(pro)

    def _connect_signals(self) -> None:
        self._save_btn.clicked.connect(self._on_save)
        self._reset_btn.clicked.connect(self._on_reset)
        self._vault_browse_btn.clicked.connect(self._on_browse_vault)
        self._suppress_warnings_check.toggled.connect(self._on_suppress_toggled)
        self._secure_container_check.toggled.connect(self._on_secure_container_toggled)
        self._gate_setup_btn.clicked.connect(self._on_gate_setup)
        self._check_updates_btn.clicked.connect(self.check_updates_requested)

    # ── Load / gather ────────────────────────────────────────────────

    def _load_into_ui(self, settings: dict) -> None:
        """Populate widgets from a settings dict."""
        serial = settings.get("serial", {})
        self._set_combo_text(self._baud_combo, str(serial.get("default_baud", 115200)))

        flash = settings.get("flash", {})
        self._set_combo_text(self._flash_baud_combo, str(flash.get("flash_baud", 921600)))

        sec = settings.get("safety", {})
        self._confirm_dangerous_check.setChecked(bool(sec.get("confirm_dangerous", True)))
        # Set the suppress box WITHOUT triggering its acknowledgement dialog.
        self._suppress_warnings_check.blockSignals(True)
        self._suppress_warnings_check.setChecked(bool(sec.get("suppress_all_warnings", False)))
        self._suppress_warnings_check.blockSignals(False)

        security = settings.get("security", {})
        self._secure_container_check.blockSignals(True)
        self._secure_container_check.setChecked(bool(security.get("secure_container", False)))
        self._secure_container_check.blockSignals(False)

        vault = settings.get("vault", {})
        self._vault_dir_edit.setText(str(vault.get("dir", "")))

        updates = settings.get("updates", {})
        self._updates_enabled_check.setChecked(bool(updates.get("enabled", True)))

    def _gather(self) -> dict:
        """Read the current UI state into a settings dict.

        Carry-forward sections this tab owns no widgets for (interface / updates bookkeeping / the
        one-time acks) are read FRESH from disk here, not from the long-lived self._settings snapshot
        taken on show. Another in-process flow (a 'Check now' that suppresses an update prompt, a
        Ctrl+M interface-mode / loadout change) can write settings.json AFTER this tab was shown — a
        modal over the already-visible tab fires no showEvent, so the snapshot goes stale. Overlaying
        only the widget-backed keys onto the current on-disk state stops a plain Save from silently
        reverting that concurrent write.
        """
        disk = load_settings()
        return {
            # Only widget-backed keys are gathered; the other keys DEFAULTS carries for these sections
            # (serial.timeout, flash.verify/auto_backup/mode, cross_comm.auto_share/dedup_by_mac) had no
            # consumer in the Qt app, so their inert controls were removed — save_settings' deep-merge
            # still restores those keys from DEFAULTS, keeping the on-disk schema stable.
            "serial": {
                "default_baud": self._parse_int(self._baud_combo.currentText(), 115200),
            },
            "flash": {
                "flash_baud": self._parse_int(self._flash_baud_combo.currentText(), 921600),
            },
            "vault": {
                "dir": self._vault_dir_edit.text().strip(),
            },
            "safety": {
                "confirm_dangerous": self._confirm_dangerous_check.isChecked(),
                "suppress_all_warnings": self._suppress_warnings_check.isChecked(),
            },
            "security": {
                "secure_container": self._secure_container_check.isChecked(),
            },
            # Preserve the one-time disclaimer ack: _gather rebuilds the whole dict,
            # so without carrying it forward a Save would reset it to False and
            # re-show the first-run disclaimer on next launch.
            "_disclaimer_ack": disk.get("_disclaimer_ack", False),
            # Same hazard for the interface section (Simple/Pro mode + the de-bloat loadout) and its
            # first-run ack — this tab owns no widgets for them, so rebuilding the dict without
            # carrying them forward makes save_settings' deep-merge reset mode to 'pro', DROP the
            # loadout, and reset _interface_mode_ack to False: a plain Save silently undoes the Simple
            # choice + loadout and re-arms both first-run choosers on the next launch.
            "interface": disk.get("interface", {}),
            "_interface_mode_ack": disk.get("_interface_mode_ack", False),
            # CRITICAL carry-forward: this tab owns only the `enabled` toggle for `updates`, but _gather
            # rebuilds the WHOLE settings dict. Without preserving the rest of the section, a plain Save
            # would wipe the suppression bookkeeping (suppressed / suppressed_at_behind / dismissed_version
            # / offline_error_suppressed / last_seen_latest / last_check_iso). Start from the on-disk block
            # and overlay only the widget-backed `enabled`.
            "updates": {
                **disk.get("updates", {}),
                "enabled": self._updates_enabled_check.isChecked(),
            },
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

    def _on_secure_container_toggled(self, checked: bool) -> None:
        """When ENABLING the secure container, point out that it depends on the access gate.

        The container's encryption key lives only inside the unlocked vault, so without a configured
        gate the container can never unseal. Warn (but don't block) so the user can set the gate up.
        """
        if not checked:
            return
        try:
            from src.security import physical_key as pk
            configured = pk.is_configured()
        except Exception:  # noqa: BLE001
            configured = False
        if not configured:
            QMessageBox.information(
                self,
                "Secure container needs the access gate",
                "The secure container is encrypted with a key held inside the access-gate vault, so it "
                "stays sealed until you set up and unlock the gate.\n\n"
                "Enable this now if you like, then set up the access gate above so saves can be "
                "encrypted. Until the gate exists/unlocks, the app falls back to its normal (plaintext) "
                "save paths.",
            )

    def _on_gate_setup(self) -> None:
        """Open the access-gate setup dialog (set admin password / physical key / policy)."""
        try:
            from src.ui.qt.gate_setup_dialog import GateSetupDialog
            GateSetupDialog(self).exec_()
        except Exception as exc:  # noqa: BLE001 — never crash Settings on the optional dialog
            log.exception("Access-gate setup dialog failed")
            QMessageBox.critical(self, "Access Gate", f"Could not open access-gate setup:\n{exc}")
        self._refresh_gate_status()

    def _refresh_gate_status(self) -> None:
        try:
            from src.security import physical_key as pk
            cfg = pk.load_config()
            self._gate_status_lbl.setText(
                f"Status: configured={pk.is_configured()}  ·  policy={cfg.get('policy')}  ·  "
                f"password={'set' if cfg.get('password') else 'not set'}  ·  "
                f"key={'set' if cfg.get('key') else 'not set'}"
            )
        except Exception:  # noqa: BLE001
            self._gate_status_lbl.setText("Status: unavailable")

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
        self._refresh_gate_status()

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
