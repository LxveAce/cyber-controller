"""Qt unlock dialog for the physical-key access gate.

Shown at startup when a gate is configured. Adapts to the configured factors (password field
and/or physical-key status) and the policy. Works under a ``--windowed`` build where a console
prompt would be invisible. Returns True only on a satisfied policy; Cancel/close returns False.
"""

from __future__ import annotations

import logging
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QApplication, QDialog, QLabel, QLineEdit, QPushButton, QVBoxLayout, QHBoxLayout,
)

from src.security import physical_key as pk

log = logging.getLogger(__name__)

_MAX_TRIES = 5


class _GateDialog(QDialog):
    def __init__(self) -> None:
        super().__init__()
        self._cfg = pk.load_config()
        self._tries = 0
        self.setWindowTitle("Cyber Controller — Locked")
        self.setModal(True)
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        policy = self._cfg.get("policy", pk.DEFAULT_POLICY)
        head = QLabel("This Cyber Controller is locked.")
        head.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(head)
        layout.addWidget(QLabel(self._policy_text(policy)))

        self._has_pw = self._cfg.get("password") is not None
        self._has_key = self._cfg.get("key") is not None

        self._pw_edit = None
        if self._has_pw:
            layout.addWidget(QLabel("Admin password:"))
            self._pw_edit = QLineEdit()
            self._pw_edit.setEchoMode(QLineEdit.Password)
            self._pw_edit.returnPressed.connect(self._attempt)
            layout.addWidget(self._pw_edit)

        self._key_label = None
        if self._has_key:
            row = QHBoxLayout()
            self._key_label = QLabel()
            row.addWidget(self._key_label, 1)
            recheck = QPushButton("Recheck key")
            recheck.clicked.connect(self._refresh_key)
            row.addWidget(recheck)
            layout.addLayout(row)
            self._refresh_key()

        self._status = QLabel("")
        self._status.setStyleSheet("color: #d9534f;")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        unlock = QPushButton("Unlock")
        unlock.setDefault(True)
        unlock.clicked.connect(self._attempt)
        btns.addWidget(unlock)
        layout.addLayout(btns)

    @staticmethod
    def _policy_text(policy: str) -> str:
        return {
            "both": "Enter the admin password AND insert the physical key.",
            "either": "Enter the admin password OR insert the physical key.",
            "password": "Enter the admin password.",
            "key": "Insert the physical key USB.",
        }.get(policy, "Authenticate to continue.")

    def _refresh_key(self) -> None:
        present = pk.key_present()
        self._key_label.setText("Physical key: " + ("detected ✓" if present else "not detected"))
        self._key_label.setStyleSheet("color: %s;" % ("#5cb85c" if present else "#aaaaaa"))

    def _attempt(self) -> None:
        pw = self._pw_edit.text() if self._pw_edit is not None else None
        granted, reason = pk.check_access(password=pw or None)
        if granted:
            self.accept()
            return
        self._tries += 1
        if self._pw_edit is not None:
            self._pw_edit.clear()
        if self._has_key:
            self._refresh_key()
        remaining = _MAX_TRIES - self._tries
        if remaining <= 0:
            self._status.setText("Too many failed attempts — exiting.")
            self.reject()
            return
        self._status.setText(f"Access denied: {reason}. {remaining} attempt(s) left.")


def unlock_gui() -> bool:
    """Show the unlock dialog. Returns True if the policy was satisfied."""
    app = QApplication.instance()
    owns_app = app is None
    if owns_app:
        app = QApplication(sys.argv)
    try:
        from src.ui.qt.theme import apply_theme
        apply_theme(app)
    except Exception:
        pass
    dlg = _GateDialog()
    dlg.setWindowModality(Qt.ApplicationModal)
    result = dlg.exec_()
    return result == QDialog.Accepted
