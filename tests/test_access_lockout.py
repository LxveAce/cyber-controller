"""Brute-force lockout + opt-in duress-wipe for the access gate (security re-harden).

All isolated via the CC_GATE_CONFIG env override (temp file), and the real wipe is spied so the
suite never touches real data.
"""

from __future__ import annotations

import pytest

pk = pytest.importorskip("src.security.physical_key")


@pytest.fixture
def temp_gate(tmp_path, monkeypatch):
    cfg = tmp_path / "access_gate.json"
    monkeypatch.setenv("CC_GATE_CONFIG", str(cfg))
    return cfg


def test_correct_password_resets_counter(temp_gate):
    pk.set_admin_password("secret")
    pk.set_policy("password")
    ok, _ = pk.check_access(password="nope")
    assert not ok and pk.lockout_status()["failed_attempts"] == 1
    ok, _ = pk.check_access(password="secret")
    assert ok and pk.lockout_status()["failed_attempts"] == 0


def test_lockout_after_threshold_blocks_even_correct_password(temp_gate):
    pk.set_admin_password("secret")
    pk.set_policy("password")
    for _ in range(pk._LOCKOUT_AFTER):
        pk.check_access(password="wrong")
    st = pk.lockout_status()
    assert st["failed_attempts"] >= pk._LOCKOUT_AFTER
    assert st["locked"] and st["remaining_secs"] > 0
    # cross-attempt + cross-restart: even the CORRECT password is refused during cooldown
    ok, reason = pk.check_access(password="secret")
    assert not ok and "locked" in reason.lower()


def test_counter_persists_across_reload(temp_gate):
    pk.set_admin_password("secret")
    pk.set_policy("password")
    pk.check_access(password="wrong")
    pk.check_access(password="wrong")
    # a fresh load_config (simulating a restart) still sees the counter
    assert int(pk.load_config().get("failed_attempts", 0)) == 2


def test_duress_wipe_triggers_at_threshold(temp_gate, monkeypatch):
    pk.set_admin_password("secret")
    pk.set_policy("password")
    pk.set_wipe_on_failures(3)
    spy = {}
    monkeypatch.setattr(pk, "trigger_duress_wipe", lambda: spy.setdefault("called", True) or True)
    results = [pk.check_access(password="wrong") for _ in range(3)]
    assert spy.get("called") is True
    assert "wipe" in results[-1][1].lower()


def test_no_wipe_when_disabled(temp_gate):
    pk.set_admin_password("secret")
    pk.set_policy("password")  # wipe_on_failures defaults to 0 (off)
    for _ in range(pk._LOCKOUT_AFTER + 2):
        pk.check_access(password="wrong")
    assert temp_gate.exists()  # config NOT wiped


def test_secure_delete_overwrites_and_removes(tmp_path):
    f = tmp_path / "secret.bin"
    f.write_bytes(b"sensitive-data" * 64)
    pk._secure_delete(f)
    assert not f.exists()


def test_console_waits_for_key_under_default_both_policy(temp_gate, tmp_path, monkeypatch):
    """Regression: a key-only gate left at the DEFAULT 'both' policy (i.e. --create-physical-key
    with no admin password) must PAUSE for the operator to insert the USB — it must not burn all
    _MAX_TRIES instantly (which trips the persistent lockout and any opt-in duress self-wipe on a
    perfectly normal boot where the key just isn't plugged in yet)."""
    import builtins
    from src.security import access_gate as ag

    pk.create_physical_key(tmp_path)  # key-only gate; policy stays the default 'both'
    assert pk.get_policy() == "both" and not pk.has_admin_password() and pk.has_physical_key()

    monkeypatch.setattr(pk, "key_present", lambda drives=None: False)  # USB not inserted
    calls = {"input": 0, "check": 0}
    real_check = pk.check_access
    monkeypatch.setattr(pk, "check_access",
                        lambda *a, **k: (calls.__setitem__("check", calls["check"] + 1), real_check(*a, **k))[1])

    def fake_input(*_a):
        calls["input"] += 1
        raise EOFError  # operator cancels instead of inserting

    monkeypatch.setattr(builtins, "input", fake_input)

    ok, pw = ag._unlock_console()
    assert ok is False and pw is None
    assert calls["input"] == 1, "must block for key insertion, not auto-fire attempts"
    assert calls["check"] == 0, "no unlock attempt should be recorded before the operator acts"
    assert int(pk.load_config().get("failed_attempts", 0)) == 0, "no spurious lockout/duress on a normal boot"
