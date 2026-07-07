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


def test_gate_setup_dialog_password_change_rekeys_vault_in_sync(qapp, monkeypatch, tmp_path):
    """Regression: changing the admin password from the GUI must re-key the vault in lock-step with the
    gate verifier (parity with access_gate.set_password_cli). The old handler committed the NEW gate
    verifier FIRST, then failed to re-key a password-only vault (no current-password prompt) — leaving
    the gate on the new password and the vault keyslot on the old one: a permanent, silent lockout."""
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "access_gate.json"))
    monkeypatch.setenv("CC_VAULT_DIR", str(tmp_path / "vault"))
    from src.ui.qt import gate_setup_dialog as gsd
    monkeypatch.setattr(gsd.QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(gsd.QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(gsd.pk, "list_removable_drives", lambda: [])

    from src.security import physical_key as pk
    from src.security import vault

    d = gsd.GateSetupDialog()
    # first-time provisioning (no existing vault -> no current-password prompt is issued)
    d._pw1.setText("old"); d._pw2.setText("old")
    d._set_password()
    assert vault.is_provisioned() and "password" in vault.factors()
    v = vault.open_vault({"password": b"old"})
    assert v is not None
    v.set("note", "launch codes")                       # prove the encrypted DATA survives a re-key

    # change old -> new: the dialog must prompt for the CURRENT password to re-key the vault
    monkeypatch.setattr(gsd.QInputDialog, "getText", lambda *a, **k: ("old", True))
    d._pw1.setText("new"); d._pw2.setText("new")
    d._set_password()

    # gate + vault BOTH moved to the new password and agree; the old opens neither; data intact
    assert pk.verify_admin_password("new") is True
    assert pk.verify_admin_password("old") is False
    v_new = vault.open_vault({"password": b"new"})
    assert v_new is not None and v_new.get("note") == "launch codes"   # same DEK, data preserved
    assert vault.open_vault({"password": b"old"}) is None


def test_gate_setup_dialog_wrong_current_password_no_desync(qapp, monkeypatch, tmp_path):
    """A password change with the WRONG (or absent) current password must fail-closed: the vault cannot
    be re-keyed, so the gate verifier must NOT be advanced — gate and vault stay on the old password."""
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "access_gate.json"))
    monkeypatch.setenv("CC_VAULT_DIR", str(tmp_path / "vault"))
    from src.ui.qt import gate_setup_dialog as gsd
    monkeypatch.setattr(gsd.QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(gsd.QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(gsd.pk, "list_removable_drives", lambda: [])

    from src.security import physical_key as pk
    from src.security import vault

    d = gsd.GateSetupDialog()
    d._pw1.setText("old"); d._pw2.setText("old")
    d._set_password()
    vault.open_vault({"password": b"old"}).set("note", "keep")

    # attempt a change with the WRONG current password -> must change NOTHING (no desync)
    monkeypatch.setattr(gsd.QInputDialog, "getText", lambda *a, **k: ("WRONG", True))
    d._pw1.setText("new"); d._pw2.setText("new")
    d._set_password()

    # the ORIGINAL password still passes the gate AND opens the vault; the new opens neither
    assert pk.verify_admin_password("old") is True
    assert pk.verify_admin_password("new") is False
    v_old = vault.open_vault({"password": b"old"})
    assert v_old is not None and v_old.get("note") == "keep"
    assert vault.open_vault({"password": b"new"}) is None


def test_settings_tab_has_gate_button(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "access_gate.json"))
    from src.ui.qt.settings_tab import SettingsTab
    t = SettingsTab()
    assert t._gate_setup_btn is not None
    assert "Status:" in t._gate_status_lbl.text()
