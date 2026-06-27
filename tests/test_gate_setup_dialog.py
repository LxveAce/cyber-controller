"""Offscreen test: the in-app access-gate SETUP dialog actually configures the password + vault."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_gate_setup_dialog_sets_and_clears(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "access_gate.json"))
    monkeypatch.setenv("CC_VAULT_DIR", str(tmp_path / "vault"))
    from src.ui.qt import gate_setup_dialog as gsd
    # neutralize modal dialogs + hardware scan
    monkeypatch.setattr(gsd.QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(gsd.QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(gsd.QMessageBox, "question", lambda *a, **k: gsd.QMessageBox.Yes)
    monkeypatch.setattr(gsd.pk, "list_removable_drives", lambda: [])

    from src.security import physical_key as pk
    from src.security import vault

    d = gsd.GateSetupDialog()
    assert pk.is_configured() is False

    # set a password through the dialog
    d._pw1.setText("s3cret!"); d._pw2.setText("s3cret!")
    d._set_password()
    assert pk.is_configured() is True
    assert pk.has_admin_password() is True
    assert vault.is_provisioned() is True
    assert pk.check_access(password="s3cret!")[0] is True
    assert pk.check_access(password="nope")[0] is False

    # policy change
    idx = d._policy.findText("password"); d._policy.setCurrentIndex(idx)
    d._apply_policy()
    assert pk.get_policy() == "password"

    # clear
    d._clear()
    assert pk.is_configured() is False


def test_settings_tab_has_gate_button(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "access_gate.json"))
    from src.ui.qt.settings_tab import SettingsTab
    t = SettingsTab()
    assert t._gate_setup_btn is not None
    assert "Status:" in t._gate_status_lbl.text()
