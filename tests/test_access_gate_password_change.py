"""Regression: changing the admin password must re-key the encrypted vault in lock-step with the gate
verifier (src/security/access_gate.set_password_cli).

The old set_password_cli committed the NEW gate verifier first (pk.set_admin_password) and only then
tried to re-key the vault with no way to supply the OLD password. For a password-only vault that
re-key raised NeedExistingFactor (silently caught), so the gate ended up on the new password while the
vault keyslot still wrapped the DEK under the old one — a permanent desync: the new password passed the
gate but could never unwrap the DEK, and the old password was rejected at the gate. Data lost for good.
"""

from __future__ import annotations

import pytest

from src.security import access_gate
from src.security import physical_key as pk
from src.security import vault


@pytest.fixture
def gate_and_vault(tmp_path, monkeypatch):
    # Isolate BOTH the gate config and the vault under a temp dir (each module reads its env live).
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "access_gate.json"))
    monkeypatch.setenv("CC_VAULT_DIR", str(tmp_path))
    return tmp_path


def _feed_getpass(monkeypatch, pairs):
    """Answer getpass by matching a substring of the prompt (order-independent, each consumed once)."""
    queue = list(pairs)

    def fake(prompt=""):
        for i, (sub, val) in enumerate(queue):
            if sub in prompt:
                return queue.pop(i)[1]
        raise AssertionError(f"unexpected getpass prompt: {prompt!r}")

    monkeypatch.setattr(access_gate.getpass, "getpass", fake)


def _provision(monkeypatch, password):
    # First-time provisioning: no existing vault, so only New + Confirm are prompted.
    _feed_getpass(monkeypatch, [("New admin password", password), ("Confirm", password)])
    assert access_gate.set_password_cli() == 0


def test_password_change_rekeys_vault_in_sync(gate_and_vault, monkeypatch):
    _provision(monkeypatch, "old")
    assert vault.open_vault({"password": b"old"}) is not None
    vault.open_vault({"password": b"old"}).set("note", "launch codes")  # prove the DATA survives

    # change old -> new (supplying the current password so the vault can be re-keyed)
    _feed_getpass(monkeypatch, [("Current admin password", "old"),
                                ("New admin password", "new"), ("Confirm", "new")])
    assert access_gate.set_password_cli() == 0

    # gate and vault now BOTH accept only the new password and agree; the old opens neither
    assert pk.verify_admin_password("new") is True
    assert pk.verify_admin_password("old") is False
    v_new = vault.open_vault({"password": b"new"})
    assert v_new is not None and v_new.get("note") == "launch codes"   # same DEK, data intact
    assert vault.open_vault({"password": b"old"}) is None


def test_wrong_current_password_aborts_without_desync(gate_and_vault, monkeypatch):
    _provision(monkeypatch, "old")
    vault.open_vault({"password": b"old"}).set("note", "keep")

    # attempt a change with the WRONG current password: it must fail-closed and change NOTHING
    _feed_getpass(monkeypatch, [("Current admin password", "WRONG"),
                                ("New admin password", "new"), ("Confirm", "new")])
    assert access_gate.set_password_cli() == 2   # nonzero: the change was refused

    # the ORIGINAL password still passes the gate AND opens the vault (gate+vault stay in sync)
    assert pk.verify_admin_password("old") is True
    assert pk.verify_admin_password("new") is False
    v_old = vault.open_vault({"password": b"old"})
    assert v_old is not None and v_old.get("note") == "keep"
