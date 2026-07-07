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


def test_concurrent_failed_attempts_are_not_lost(temp_gate):
    """SEC-A2: the failure counter must be a lost-update-safe read-modify-write. Many attempts
    firing at once (threads here; separate relaunch-and-guess processes in the wild) previously
    raced load→increment→save and dropped increments, so the counter never reached the lockout
    threshold. Under the atomic update every increment must land."""
    import threading

    pk.set_admin_password("secret")
    pk.set_policy("password")
    n = 25
    start = threading.Barrier(n)

    def worker():
        start.wait()  # release all threads at once to maximise interleaving
        pk.record_failed_attempt()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert pk.lockout_status()["failed_attempts"] == n


def test_corrupt_gate_config_fails_closed(temp_gate):
    """SEC-C2: a present-but-unreadable gate config must NOT be treated as 'no gate configured'
    (which grants access). It must fail closed — otherwise corrupting the file bypasses the gate."""
    pk.set_admin_password("secret")
    pk.set_policy("password")
    temp_gate.write_text("}{ this is not json", encoding="utf-8")  # corrupt the configured gate

    assert pk.config_is_corrupt() is True
    ok, reason = pk.check_access(password="secret")
    assert ok is False and "corrupt" in reason.lower()
    ok, _ = pk.check_access(password="")          # no blank-grant either
    assert ok is False


def test_absent_gate_config_is_still_a_noop(temp_gate):
    """A truly-absent config (fresh install) must remain a no-op that grants — only a CORRUPT
    (present-but-unreadable) config fails closed, so a new user is never locked out."""
    assert not temp_gate.exists()
    assert pk.config_is_corrupt() is False
    ok, reason = pk.check_access()
    assert ok is True and "no gate" in reason.lower()


def test_secure_delete_overwrites_and_removes(tmp_path):
    f = tmp_path / "secret.bin"
    f.write_bytes(b"sensitive-data" * 64)
    assert pk._secure_delete(f) is True   # verifiably gone
    assert not f.exists()


def test_secure_delete_reports_false_when_file_cannot_be_removed(tmp_path, monkeypatch):
    """SEC wrong-success-on-error: a best-effort secure delete that cannot actually remove the file
    (held open / read-only / ACL) must return False — it must NOT swallow the error and imply success.
    An anti-forensic control has to be able to tell the caller the secret is still on disk."""
    import pathlib

    f = tmp_path / "secret.bin"
    f.write_bytes(b"sensitive-data" * 64)

    def _boom(*_a, **_k):
        raise PermissionError("held open")

    # Both the primary os.remove and the fallback path.unlink fail -> file survives.
    monkeypatch.setattr(pk.os, "remove", _boom)
    monkeypatch.setattr(pathlib.Path, "unlink", _boom)

    assert pk._secure_delete(f) is False   # was None (falsely swallowed) before the fix
    assert f.exists()                      # the file genuinely remains on disk


def test_duress_wipe_reports_false_when_a_secret_survives(temp_gate, tmp_path, monkeypatch):
    """SEC wrong-success-on-error: if a targeted secret cannot actually be destroyed (held open /
    read-only / ACL), trigger_duress_wipe must return False and check_access must NOT announce a
    'secure wipe'. Previously it flagged wiped=True unconditionally, so the owner was told their
    secrets were destroyed while recoverable ciphertext was still on disk."""
    import pathlib
    from src.security import vault

    data = tmp_path / "vault.enc"
    data.write_bytes(b"ciphertext-that-must-not-survive" * 64)
    hdr = tmp_path / "vault.hdr.json"  # absent -> skipped by the wipe
    monkeypatch.setattr(vault, "_data_path", lambda: data)
    monkeypatch.setattr(vault, "_hdr_path", lambda: hdr)

    pk.set_admin_password("secret")   # gives _config_path() a real (removable) secret to target
    pk.set_policy("password")

    real_remove = pk.os.remove
    real_unlink = pathlib.Path.unlink

    def _blocked_remove(p, *a, **k):
        if pathlib.Path(p) == data:
            raise PermissionError("vault held open")
        return real_remove(p, *a, **k)

    def _blocked_unlink(self, *a, **k):
        if pathlib.Path(self) == data:
            raise PermissionError("vault held open")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(pk.os, "remove", _blocked_remove)
    monkeypatch.setattr(pathlib.Path, "unlink", _blocked_unlink)

    # Direct: the wipe of the still-locked vault must be reported as a failure, not a success.
    assert pk.trigger_duress_wipe() is False
    assert data.exists(), "the vault ciphertext could not be deleted (as set up)"


def test_check_access_does_not_report_wipe_that_did_not_happen(temp_gate, tmp_path, monkeypatch):
    """End-to-end repro of the finding: drive the failed-attempt threshold with a secret that cannot
    be deleted. check_access must not return 'secure wipe triggered' (nor set wipe_triggered) while the
    ciphertext is still on disk."""
    import pathlib
    from src.security import vault

    data = tmp_path / "vault.enc"
    data.write_bytes(b"ciphertext" * 128)
    hdr = tmp_path / "vault.hdr.json"
    monkeypatch.setattr(vault, "_data_path", lambda: data)
    monkeypatch.setattr(vault, "_hdr_path", lambda: hdr)

    pk.set_admin_password("secret")
    pk.set_policy("password")
    pk.set_wipe_on_failures(3)

    real_remove = pk.os.remove
    real_unlink = pathlib.Path.unlink

    def _blocked_remove(p, *a, **k):
        if pathlib.Path(p) == data:
            raise PermissionError("vault held open")
        return real_remove(p, *a, **k)

    def _blocked_unlink(self, *a, **k):
        if pathlib.Path(self) == data:
            raise PermissionError("vault held open")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(pk.os, "remove", _blocked_remove)
    monkeypatch.setattr(pathlib.Path, "unlink", _blocked_unlink)

    results = [pk.check_access(password="wrong") for _ in range(3)]
    last_ok, last_reason = results[-1]

    assert last_ok is False
    assert data.exists(), "the vault ciphertext survived the blocked delete"
    assert "wipe" not in last_reason.lower(), "must not claim a secure wipe that did not happen"


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
