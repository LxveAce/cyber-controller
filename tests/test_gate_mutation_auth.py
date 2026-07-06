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


def test_gate_policy_change_requires_auth(configured_gate, monkeypatch):
    # Coverage: every mutating subcommand on a configured gate must pass enforce(), not just clear/set-pw.
    calls = {"enforce": 0, "pol": 0}
    monkeypatch.setattr(access_gate, "enforce", lambda ui: calls.__setitem__("enforce", calls["enforce"] + 1) or False)
    monkeypatch.setattr(access_gate, "set_policy_cli", lambda p: calls.__setitem__("pol", calls["pol"] + 1) or 0)
    rc = app.main(["--gate-policy", "either"])
    assert calls["enforce"] == 1 and calls["pol"] == 0, "changing policy on a configured gate must require auth"
    assert rc != 0


def test_create_physical_key_requires_auth(configured_gate, monkeypatch):
    calls = {"enforce": 0, "key": 0}
    monkeypatch.setattr(access_gate, "enforce", lambda ui: calls.__setitem__("enforce", calls["enforce"] + 1) or False)
    monkeypatch.setattr(access_gate, "create_key_cli", lambda *a, **k: calls.__setitem__("key", calls["key"] + 1) or 0)
    rc = app.main(["--create-physical-key"])
    assert calls["enforce"] == 1 and calls["key"] == 0, "adding a key to a configured gate must require auth"
    assert rc != 0


def test_mutation_runs_when_auth_passes_on_configured_gate(configured_gate, monkeypatch):
    # The auth-PASS path (not only the deny path): a configured-gate mutation proceeds once enforce() succeeds.
    calls = {"enforce": 0, "setpw": 0}
    monkeypatch.setattr(access_gate, "enforce", lambda ui: calls.__setitem__("enforce", calls["enforce"] + 1) or True)
    monkeypatch.setattr(access_gate, "set_password_cli", lambda: calls.__setitem__("setpw", calls["setpw"] + 1) or 0)
    rc = app.main(["--set-admin-password"])
    assert calls["enforce"] == 1 and calls["setpw"] == 1, "an authenticated mutation on a configured gate must run"
    assert rc == 0


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


def test_cli_subcommand_runs_despite_single_instance_lock(tmp_path, monkeypatch):
    # The single-instance lock guards only the interactive GUI launch. A headless one-shot CLI op must run
    # even when a GUI instance holds the lock — otherwise (e.g.) the DMS setup the GUI directs the user to
    # run while it's open would be a silent no-op returning success.
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "access_gate.json"))
    monkeypatch.setattr(app, "_acquire_instance_lock", lambda: False)  # simulate a running GUI holding it
    calls = {"status": 0}
    monkeypatch.setattr(access_gate, "status_cli", lambda: calls.__setitem__("status", calls["status"] + 1) or 0)
    rc = app.main(["--gate-status"])
    assert calls["status"] == 1, "a CLI subcommand must run despite the instance lock"
    assert rc == 0


def test_disarm_duress_wipe_clears_threshold_and_counters(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "access_gate.json"))
    pk.set_wipe_on_failures(3)
    pk.record_failed_attempt(allow_wipe=True)  # bump the counters too
    pk.disarm_duress_wipe()
    cfg = pk.load_config()
    assert cfg.get("wipe_on_failures", 0) == 0
    assert cfg.get("wipe_failures", 0) == 0
    assert cfg.get("failed_attempts", 0) == 0


def test_clear_gate_disarms_the_duress_wipe(configured_gate, monkeypatch):
    # Opt into the destructive wipe, then clear the gate (authenticated). The threshold must NOT survive to
    # silently re-arm a later reprovisioned gate the owner never re-opted into.
    pk.set_wipe_on_failures(3)
    monkeypatch.setattr(access_gate, "enforce", lambda ui: True)  # authenticated clear
    assert app.main(["--clear-gate"]) == 0
    assert pk.load_config().get("wipe_on_failures", 0) == 0, "a gate clear must disarm the duress wipe"


# ── CC-GATE: a corrupt (present-but-unreadable) gate config must NOT fail open ──────────────────────
# load_config() marks an unparseable config `__corrupt__` → is_configured() is False but
# config_is_corrupt() is True. The old `_gate_mutation and is_configured()` guard therefore SKIPPED
# enforce() on a corrupt config and ran the mutation pre-auth (fail-open). The fix treats a corrupt
# config as configured-and-locked: every mutation is blocked EXCEPT --clear-gate (the recovery path,
# since enforce() itself can't authenticate a config it can't parse).

@pytest.fixture
def corrupt_gate(tmp_path, monkeypatch, no_instance_lock):
    cfg = tmp_path / "access_gate.json"
    cfg.write_text("{ this is NOT valid json", encoding="utf-8")  # present but unparseable
    monkeypatch.setenv("CC_GATE_CONFIG", str(cfg))
    assert not pk.is_configured(), "the bug's trigger: a corrupt config reads as 'not configured'"
    assert pk.config_is_corrupt(), "...but it IS a present, unreadable gate — must fail closed"


def test_corrupt_config_blocks_set_password_no_bypass(corrupt_gate, monkeypatch):
    # enforce() is stubbed to True (worst case): even if it were reached and 'passed', the mutation must
    # NOT run on a corrupt config — the guard blocks before enforce, so set_password_cli never fires.
    calls = {"enforce": 0, "setpw": 0}
    monkeypatch.setattr(access_gate, "enforce", lambda ui: calls.__setitem__("enforce", calls["enforce"] + 1) or True)
    monkeypatch.setattr(access_gate, "set_password_cli", lambda: calls.__setitem__("setpw", calls["setpw"] + 1) or 0)
    rc = app.main(["--set-admin-password"])
    assert calls["setpw"] == 0, "a mutation on a corrupt config must NOT run (was fail-open — CC-GATE)"
    assert rc != 0, "a BLOCKED mutation must exit nonzero — a script checking $? must not read success"


def test_corrupt_config_blocks_gate_policy(corrupt_gate, monkeypatch):
    calls = {"pol": 0}
    monkeypatch.setattr(access_gate, "set_policy_cli", lambda p: calls.__setitem__("pol", calls["pol"] + 1) or 0)
    rc = app.main(["--gate-policy", "either"])
    assert calls["pol"] == 0, "policy change must be blocked on a corrupt config"
    assert rc != 0


def test_corrupt_config_blocks_create_physical_key(corrupt_gate, monkeypatch):
    calls = {"key": 0}
    monkeypatch.setattr(access_gate, "create_key_cli", lambda *a, **k: calls.__setitem__("key", calls["key"] + 1) or 0)
    rc = app.main(["--create-physical-key"])
    assert calls["key"] == 0, "adding a key must be blocked on a corrupt config"
    assert rc != 0


def test_corrupt_config_allows_clear_gate_recovery_without_enforce(corrupt_gate, monkeypatch):
    # --clear-gate is the ONE mutation allowed on a corrupt config: enforce() can't authenticate an
    # unreadable config, so requiring it would brick recovery. It must run WITHOUT calling enforce().
    calls = {"enforce": 0, "clear": 0}
    monkeypatch.setattr(access_gate, "enforce", lambda ui: calls.__setitem__("enforce", calls["enforce"] + 1) or False)
    monkeypatch.setattr(access_gate, "clear_cli", lambda: calls.__setitem__("clear", calls["clear"] + 1) or 0)
    rc = app.main(["--clear-gate"])
    assert calls["clear"] == 1, "--clear-gate must proceed as the recovery path on a corrupt config"
    assert calls["enforce"] == 0, "enforce() can't auth a corrupt config; recovery must not call it"
    assert rc == 0


def test_corrupt_config_clear_plus_other_mutation_is_blocked(corrupt_gate, monkeypatch):
    # BYPASS guard: the action dispatch runs --create/--set-admin-password/--gate-policy BEFORE --clear-gate,
    # so co-passing a mutation with --clear-gate on a corrupt config must NOT let it wave through pre-auth
    # under cover of the recovery path. The whole combo is blocked; only a PURE --clear-gate recovers.
    calls = {"setpw": 0, "clear": 0}
    monkeypatch.setattr(access_gate, "set_password_cli", lambda: calls.__setitem__("setpw", calls["setpw"] + 1) or 0)
    monkeypatch.setattr(access_gate, "clear_cli", lambda: calls.__setitem__("clear", calls["clear"] + 1) or 0)
    rc = app.main(["--clear-gate", "--set-admin-password"])
    assert calls["setpw"] == 0, "co-passed set-password must NOT run pre-auth on a corrupt config (bypass)"
    assert calls["clear"] == 0, "the whole combo is blocked; --clear-gate must be requested on its own"
    assert rc != 0


def test_corrupt_config_clear_gate_end_to_end_resets_config(corrupt_gate):
    # Real recovery (nothing stubbed): --clear-gate rewrites the unreadable file to a clean unconfigured
    # config, so afterward it is neither corrupt nor configured and the owner can reprovision.
    rc = app.main(["--clear-gate"])
    assert rc == 0
    assert not pk.config_is_corrupt(), "clearing must rewrite the corrupt file to a valid one"
    assert not pk.is_configured(), "after recovery the gate is unconfigured (ready to reprovision)"
