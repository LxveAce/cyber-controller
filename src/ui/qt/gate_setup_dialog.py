"""Access-gate SETUP dialog (Qt) — configure the admin password / physical key from inside the app.

Until now the gate could only be UNLOCKED from the GUI (access_gate_dialog) and SET UP from the CLI
(``--set-admin-password`` / ``--create-physical-key`` / ``--gate-policy``). This dialog gives the app
itself the ability to set up the password feature: it drives the same hardened backend
(:mod:`src.security.physical_key` + the gate-keyed :mod:`src.security.vault`), so a password/key set
here is a salted scrypt verifier (no plaintext) and provisions the encrypted-vault keyslot.

Changes take effect on the next launch (the gate is enforced at startup, before any UI loads).
Owner-only, defensive use on hardware you own.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from src.security import physical_key as pk
from src.security import vault

log = logging.getLogger(__name__)


class GateSetupDialog(QDialog):
    """Set/clear the admin password, create a physical key, and choose the gate policy."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Access Gate — password / physical key setup")
        self.setMinimumWidth(460)
        self._build_ui()
        self._refresh()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        intro = QLabel("Lock Cyber Controller behind an admin password and/or a physical USB key. "
                       "Secrets are stored as salted hashes (no plaintext) and the protected vault stays "
                       "encrypted until unlocked. Changes apply on the next launch. Owner-only use.")
        intro.setWordWrap(True)
        root.addWidget(intro)

        self._status = QLabel()
        self._status.setObjectName("muted")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        root.addWidget(self._hline())

        # Policy
        prow = QHBoxLayout()
        prow.addWidget(QLabel("Policy:"))
        self._policy = QComboBox()
        self._policy.addItems(list(pk.POLICIES))
        self._policy.setToolTip("both = password AND key · either = password OR key · password-only · key-only")
        prow.addWidget(self._policy, 1)
        btn_policy = QPushButton("Apply policy")
        btn_policy.clicked.connect(self._apply_policy)
        prow.addWidget(btn_policy)
        root.addLayout(prow)

        # Admin password
        root.addWidget(self._label("Admin password"))
        self._pw1 = QLineEdit(); self._pw1.setEchoMode(QLineEdit.Password)
        self._pw1.setPlaceholderText("New admin password")
        self._pw2 = QLineEdit(); self._pw2.setEchoMode(QLineEdit.Password)
        self._pw2.setPlaceholderText("Confirm password")
        root.addWidget(self._pw1)
        root.addWidget(self._pw2)
        btn_pw = QPushButton("Set / change admin password")
        btn_pw.setToolTip("Stores a salted scrypt verifier and provisions the encrypted-vault keyslot.")
        btn_pw.clicked.connect(self._set_password)
        root.addWidget(btn_pw)

        # Physical key
        root.addWidget(self._label("Physical USB key"))
        krow = QHBoxLayout()
        self._drive = QComboBox()
        self._drive.setToolTip("Removable drive to provision as an unlock key (writes a key file to it).")
        krow.addWidget(self._drive, 1)
        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self._refresh_drives)
        krow.addWidget(btn_refresh)
        root.addLayout(krow)
        btn_key = QPushButton("Create physical key on this drive")
        btn_key.clicked.connect(self._create_key)
        root.addWidget(btn_key)

        root.addWidget(self._hline())

        # Brute-force / duress self-wipe (opt-in, destructive)
        root.addWidget(self._label("Duress self-wipe (advanced — destructive)"))
        wrow = QHBoxLayout()
        wrow.addWidget(QLabel("Wipe app secrets after N failed unlocks (0 = off):"))
        self._wipe_spin = QSpinBox()
        self._wipe_spin.setRange(0, 100)
        self._wipe_spin.setSpecialValueText("off")
        self._wipe_spin.setToolTip(
            "After this many CONSECUTIVE wrong unlocks the app securely wipes its OWN secrets (gate "
            "config + encrypted vault). Off by default. Set it high enough that ordinary typos won't "
            "trip it. A persistent counter + cooldown also throttles brute force regardless of this.")
        wrow.addWidget(self._wipe_spin, 1)
        btn_wipe = QPushButton("Apply")
        btn_wipe.clicked.connect(self._apply_wipe)
        wrow.addWidget(btn_wipe)
        root.addLayout(wrow)
        wnote = QLabel("When triggered this destroys THIS app's secrets only (never other files) and "
                       "cannot be undone. Honest limit: on SSDs, overwrite is not a forensic guarantee.")
        wnote.setObjectName("muted"); wnote.setWordWrap(True)
        root.addWidget(wnote)

        root.addWidget(self._hline())

        brow = QHBoxLayout()
        self._btn_clear = QPushButton("Clear gate (remove password + key)")
        self._btn_clear.setObjectName("erase_btn")
        self._btn_clear.clicked.connect(self._clear)
        brow.addWidget(self._btn_clear)
        brow.addStretch()
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        brow.addWidget(btn_close)
        root.addLayout(brow)

    @staticmethod
    def _label(text: str) -> QLabel:
        lbl = QLabel(f"<b>{text}</b>")
        return lbl

    @staticmethod
    def _hline() -> QFrame:
        ln = QFrame(); ln.setFrameShape(QFrame.HLine); ln.setFrameShadow(QFrame.Sunken)
        return ln

    # ── state ────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        cfg = pk.load_config()
        idx = self._policy.findText(cfg.get("policy", pk.DEFAULT_POLICY))
        if idx >= 0:
            self._policy.setCurrentIndex(idx)
        kid = (cfg.get("key") or {}).get("key_id") if cfg.get("key") else None
        prov = (("provisioned: " + ", ".join(vault.factors())) if vault.is_provisioned() else "none")
        wipe_n = int(cfg.get("wipe_on_failures", 0) or 0)
        self._wipe_spin.setValue(wipe_n)
        self._status.setText(
            f"Configured: {pk.is_configured()}   ·   policy: {cfg.get('policy')}   ·   "
            f"password: {'set' if cfg.get('password') else 'not set'}   ·   "
            f"key: {('set (' + kid + ')') if kid else 'not set'}   ·   encrypted vault: {prov}   ·   "
            f"duress wipe: {('after ' + str(wipe_n) + ' fails') if wipe_n else 'off'}"
        )
        self._refresh_drives()

    def _refresh_drives(self) -> None:
        self._drive.clear()
        for d in pk.list_removable_drives():
            self._drive.addItem(str(d), str(d))
        if self._drive.count() == 0:
            self._drive.addItem("(no removable drives detected)", None)

    # ── actions ──────────────────────────────────────────────────────

    def _apply_policy(self) -> None:
        try:
            pk.set_policy(self._policy.currentText())
            QMessageBox.information(self, "Access gate", f"Policy set to '{self._policy.currentText()}'.")
        except ValueError as exc:
            QMessageBox.warning(self, "Access gate", str(exc))
        self._refresh()

    def _apply_wipe(self) -> None:
        n = self._wipe_spin.value()
        if n > 0 and QMessageBox.warning(
                self, "Duress self-wipe",
                f"Enable duress self-wipe after {n} consecutive failed unlocks?\n\n"
                "When triggered this SECURELY DESTROYS this app's secrets (gate config + encrypted "
                "vault) and CANNOT be undone. Continue?",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Yes:
            return
        pk.set_wipe_on_failures(n)
        QMessageBox.information(
            self, "Access gate",
            (f"Duress wipe enabled at {n} failed attempts." if n else "Duress wipe disabled.")
            + " Applies immediately.")
        self._refresh()

    def _set_password(self) -> None:
        pw, pw2 = self._pw1.text(), self._pw2.text()
        if not pw or pw != pw2:
            QMessageBox.warning(self, "Access gate", "Passwords are empty or do not match.")
            return
        pk.set_admin_password(pw)
        unlock = {}
        if pk.has_physical_key():
            ks = pk.present_key_secret()
            if ks:
                unlock["key"] = ks
        try:
            vault.set_factor("password", pw.encode("utf-8"), unlock_with=unlock or None)
            msg = "Admin password set; encrypted-vault keyslot provisioned. Applies on next launch."
        except vault.NeedExistingFactor:
            msg = ("Admin password set, but the vault keyslot was not added because the existing "
                   "physical key is not present. Insert the key and set the password again to add it.")
        self._pw1.clear(); self._pw2.clear()
        QMessageBox.information(self, "Access gate", msg)
        self._refresh()

    def _create_key(self) -> None:
        drive = self._drive.currentData()
        if not drive:
            QMessageBox.warning(self, "Access gate", "Select a removable drive first.")
            return
        if QMessageBox.question(
                self, "Create physical key",
                f"Write a Cyber Controller key file to:\n\n    {drive}\n\nKeep this USB safe — anyone with "
                "the file holds the key. Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            kid = pk.create_physical_key(drive)
        except (OSError, NotADirectoryError) as exc:
            QMessageBox.critical(self, "Access gate", f"Failed to write the key to {drive}:\n{exc}")
            return
        secret = pk._read_key_secret(Path(drive) / pk.KEY_FILENAME)
        unlock = {}
        if pk.has_admin_password():
            pwd, ok = QInputDialog.getText(
                self, "Admin password",
                "Enter the existing admin password to add this key to the encrypted vault:",
                QLineEdit.Password)
            if ok and pwd:
                unlock["password"] = pwd.encode("utf-8")
        try:
            if secret is not None:
                vault.set_factor("key", secret, unlock_with=unlock or None)
            QMessageBox.information(self, "Access gate",
                                   f"Physical key {kid} created on {drive}. Applies on next launch.")
        except vault.NeedExistingFactor:
            QMessageBox.warning(self, "Access gate",
                                "Key written to the USB, but the vault keyslot needs the admin password. "
                                "Re-create the key and enter the admin password to add it.")
        self._refresh()

    def _clear(self) -> None:
        if QMessageBox.question(
                self, "Clear access gate",
                "Remove the admin password and physical key so the app starts without prompting?\n\n"
                "(The encrypted vault file is left in place — its data stays encrypted.)",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        pk.clear_admin_password()
        pk.remove_physical_key()
        pk.disarm_duress_wipe()  # clearing the gate must also disarm the opt-in wipe (parity with the CLI)
        QMessageBox.information(self, "Access gate", "Access gate cleared. Applies on next launch.")
        self._refresh()
