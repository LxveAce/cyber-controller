"""PyQt5 dialog — Dead Man's Switch password & duress setup (host-side, "Approach A").

Owner-only DEFENSIVE provisioning for hardware you own. The boot password is hashed **host-side**
(PBKDF2-HMAC-SHA256) and the buffer is **zeroized** by the provisioner — it is never stored, logged,
or sent to the device (only {salt, pwhash, params} reach the board). A disarmed or unprovisioned
board can NEVER wipe (fail-safe). Set the password here BEFORE flashing the DMS build; this
mints ``guardcfg.bin`` + a flash bundle manifest via :func:`src.core.suicide_setup.build`.
"""

from __future__ import annotations

import logging
import os

from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.core.suicide_setup import SuicideConfig, build

log = logging.getLogger(__name__)


def _path_row(line_edit: QLineEdit, on_browse) -> QWidget:
    """A line edit plus a browse button, packed into one row widget for QFormLayout."""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(line_edit)
    btn = QPushButton("…")
    btn.setMaximumWidth(32)
    btn.clicked.connect(on_browse)
    row.addWidget(btn)
    w = QWidget()
    w.setLayout(row)
    return w


class SuicideSetupDialog(QDialog):
    """Collects target hardware, duress config, and a boot password; bakes the DMS guardcfg bundle."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Dead Man's Switch — Password & Duress Setup")
        self.setMinimumWidth(540)
        self._build_ui()

    # ── UI ───────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        warn = QLabel(
            "Owner-only DEFENSIVE provisioning for hardware you own. The password is hashed "
            "host-side (PBKDF2-HMAC-SHA256) and is never stored, logged, or sent to the device. "
            "A disarmed or unprovisioned board can NEVER wipe."
        )
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #39ff14;")
        root.addWidget(warn)

        # Target hardware
        hw = QGroupBox("Target hardware")
        hwf = QFormLayout(hw)
        self.chip = QComboBox()
        self.chip.addItems(["esp32", "esp32s2", "esp32s3", "esp32c3", "esp32c6", "esp32h2"])
        self.flash = QComboBox()
        self.flash.addItems(["4MB", "8MB", "16MB"])
        self.variant = QComboBox()
        self.variant.addItems(["fork", "guardian"])
        hwf.addRow("Chip:", self.chip)
        hwf.addRow("Flash size:", self.flash)
        hwf.addRow("Variant:", self.variant)
        root.addWidget(hw)

        # Duress / arming
        du = QGroupBox("Duress / arming")
        duf = QFormLayout(du)
        self.arm_pin = QSpinBox()
        self.arm_pin.setRange(0, 48)
        self.arm_pin.setValue(27)
        self.arm_level = QComboBox()
        self.arm_level.addItems(["HIGH = armed (1)", "LOW = armed (0)"])
        self.max_att = QSpinBox()
        self.max_att.setRange(1, 10)
        self.max_att.setValue(2)
        self.armed = QCheckBox("ARM this board now — it WILL self-destruct on the configured trigger")
        self.brick = QCheckBox("Brick boot chain on wipe (T2 — NOT reflashable)")
        duf.addRow("Arming GPIO:", self.arm_pin)
        duf.addRow("Armed level:", self.arm_level)
        duf.addRow("Attempts before wipe:", self.max_att)
        duf.addRow(self.armed)
        duf.addRow(self.brick)
        root.addWidget(du)

        # Boot password
        pw = QGroupBox("Boot password")
        pwf = QFormLayout(pw)
        self.pw1 = QLineEdit()
        self.pw1.setEchoMode(QLineEdit.Password)
        self.pw2 = QLineEdit()
        self.pw2.setEchoMode(QLineEdit.Password)
        self.show_pw = QCheckBox("Show password")
        self.show_pw.toggled.connect(self._toggle_echo)
        pwf.addRow("Password:", self.pw1)
        pwf.addRow("Confirm:", self.pw2)
        pwf.addRow("", self.show_pw)
        root.addWidget(pw)

        # Output / firmware build dir
        io = QGroupBox("Output")
        iof = QFormLayout(io)
        self.out_dir = QLineEdit(os.path.abspath("suicide_bundle"))
        self.build_dir = QLineEdit("")
        self.build_dir.setPlaceholderText("(optional) dir with built firmware .bins — blank = guardcfg only")
        iof.addRow("Bundle out:", _path_row(self.out_dir, self._browse_out))
        iof.addRow("Firmware build dir:", _path_row(self.build_dir, self._browse_build))
        root.addWidget(io)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        provision = QPushButton("Provision Bundle")
        provision.setDefault(True)
        provision.clicked.connect(self._on_provision)
        btn_row.addWidget(cancel)
        btn_row.addWidget(provision)
        root.addLayout(btn_row)

    # ── Slots ────────────────────────────────────────────────────────

    def _toggle_echo(self, show: bool) -> None:
        mode = QLineEdit.Normal if show else QLineEdit.Password
        self.pw1.setEchoMode(mode)
        self.pw2.setEchoMode(mode)

    def _browse_out(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Bundle output directory", self.out_dir.text())
        if d:
            self.out_dir.setText(d)

    def _browse_build(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Firmware build directory", self.build_dir.text())
        if d:
            self.build_dir.setText(d)

    def _collect_cfg(self) -> SuicideConfig:
        return SuicideConfig(
            chip=self.chip.currentText(),
            flash_size=self.flash.currentText(),
            variant=self.variant.currentText(),
            arm_pin=self.arm_pin.value(),
            arm_level=1 if self.arm_level.currentIndex() == 0 else 0,
            max_att=self.max_att.value(),
            armed=1 if self.armed.isChecked() else 0,
            brick=1 if self.brick.isChecked() else 0,
            build_dir=self.build_dir.text().strip(),
        )

    def _on_provision(self) -> None:
        p1 = self.pw1.text()
        p2 = self.pw2.text()
        if not p1 or p1 != p2:
            QMessageBox.warning(self, "Password", "Passwords are empty or do not match.")
            return

        if self.armed.isChecked():
            r = QMessageBox.warning(
                self,
                "Confirm ARM",
                "armed=1 means this board WILL self-destruct on the configured trigger.\n\n"
                "All selected memory regions will be wiped and overwritten, leaving no trace. "
                "Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if r != QMessageBox.Yes:
                return

        cfg = self._collect_cfg()
        out = self.out_dir.text().strip() or os.path.abspath("suicide_bundle")
        try:
            out_dir, manifest, warnings = build(cfg, p1, out)
        except Exception as exc:  # noqa: BLE001 — surface any provisioner error to the user
            log.exception("Suicide provisioning failed")
            QMessageBox.critical(self, "Provisioning failed", str(exc))
            return
        finally:
            # Best-effort: clear the visible fields. Python/Qt strings are immutable so the plaintext
            # can't be zeroized here, but the provisioner zeroizes the password bytearray it hashed.
            self.pw1.clear()
            self.pw2.clear()
            p1 = p2 = None

        msg = (
            f"Provisioned bundle:\n{out_dir}\n\n"
            f"guardcfg.bin minted (PBKDF2-HMAC-SHA256, iter={cfg.kdf_iter}); password hashed + zeroized.\n"
            f"armed={cfg.armed}  arm_pin={cfg.arm_pin}  arm_level={cfg.arm_level}  "
            f"max_att={cfg.max_att}  brick={cfg.brick}"
        )
        if warnings:
            msg += (
                f"\n\nNOTE: {len(warnings)} firmware image(s) not present — build the Suicide-Marauder "
                "firmware (set the build dir) to complete the flash bundle, then flash via flash_suicide."
            )
        if cfg.armed == 1:
            msg += "\n\n*** armed=1: this board will self-destruct on the configured trigger conditions. ***"
        QMessageBox.information(self, "Done", msg)
        self.accept()
