"""Boot-attack resistance: mutating an already-configured access gate must pass the gate first.

Closes the pre-auth startup bypass where `--clear-gate` / `--set-admin-password` could reset or
disable the gate without knowing the current factor(s). First-time setup (no gate yet) needs no auth.
All isolated via CC_GATE_CONFIG; the single-instance lock and the actual CLI actions are stubbed so
nothing real runs.
"""

from __future__ import annotations

import pytest

pk = pytest.importorskip("src.security.physical_key")
from src.security import access_gate  # noqa: E402
import src.app as app  # noqa: E402


@pytest.fixture
def no_instance_lock(monkeypatch):
    monkeypatch.setattr(app, "_acquire_instance_lock", lambda: True)


@pytest.fixture
def configured_gate(tmp_path, monkeypatch, no_instance_lock):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "access_gate.json"))
    pk.set_admin_password("secret")
    assert pk.is_configured()


def test_clear_gate_denied_when_auth_fails(configured_gate, monkeypatch):
    calls = {"enforce": 0, "clear": 0}
    monkeypatch.setattr(access_gate, "enforce", lambda ui: calls.__setitem__("enforce", calls["enforce"] + 1) or False)
    monkeypatch.setattr(access_gate, "clear_cli", lambda: calls.__setitem__("clear", calls["clear"] + 1) or 0)
    rc = app.main(["--clear-gate"])
    assert calls["enforce"] == 1, "the gate must be challenged before a mutation"
    assert calls["clear"] == 0, "clear must NOT run when auth is denied (no pre-auth bypass)"
    assert rc != 0, "a BLOCKED mutation must exit nonzero — a script checking $? must not read success"


def test_clear_gate_allowed_when_auth_passes(configured_gate, monkeypatch):
    calls = {"enforce": 0, "clear": 0}
    monkeypatch.setattr(access_gate, "enforce", lambda ui: calls.__setitem__("enforce", calls["enforce"] + 1) or True)
    monkeypatch.setattr(access_gate, "clear_cli", lambda: calls.__setitem__("clear", calls["clear"] + 1) or 0)
    rc = app.main(["--clear-gate"])
    assert calls["enforce"] == 1
    assert calls["clear"] == 1
    assert rc == 0


def test_set_password_change_requires_auth(configured_gate, monkeypatch):
    calls = {"enforce": 0, "setpw": 0}
    monkeypatch.setattr(access_gate, "enforce", lambda ui: calls.__setitem__("enforce", calls["enforce"] + 1) or False)
    monkeypatch.setattr(access_gate, "set_password_cli", lambda: calls.__setitem__("setpw", calls["setpw"] + 1) or 0)
    app.main(["--set-admin-password"])
    assert calls["enforce"] == 1 and calls["setpw"] == 0


def test_first_time_setup_needs_no_auth(tmp_path, monkeypatch, no_instance_lock):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "access_gate.json"))
    assert not pk.is_configured()  # nothing configured yet
    calls = {"enforce": 0, "setpw": 0}
    monkeypatch.setattr(access_gate, "enforce", lambda ui: calls.__setitem__("enforce", calls["enforce"] + 1) or True)
    monkeypatch.setattr(access_gate, "set_password_cli", lambda: calls.__setitem__("setpw", calls["setpw"] + 1) or 0)
    rc = app.main(["--set-admin-password"])
    assert calls["enforce"] == 0, "first-time setup must not require pre-auth"
    assert calls["setpw"] == 1
    assert rc == 0


def test_gate_status_is_readonly_no_auth(configured_gate, monkeypatch):
    calls = {"enforce": 0, "status": 0}
    monkeypatch.setattr(access_gate, "enforce", lambda ui: calls.__setitem__("enforce", calls["enforce"] + 1) or True)
    monkeypatch.setattr(access_gate, "status_cli", lambda: calls.__setitem__("status", calls["status"] + 1) or 0)
    app.main(["--gate-status"])
    assert calls["enforce"] == 0, "read-only status must never require auth"
    assert calls["status"] == 1
