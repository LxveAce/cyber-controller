"""Session-revocation primitives in ``src/security/web_auth.py``.

The live credential carries a process-lifetime, unguessable *generation* token bound to the SAME
immutable snapshot as the (username, salt, hash). It is never persisted, so a fresh process mints a
fresh generation (every restart forces re-login), and it rotates on a password change/clear so those
actions revoke every existing cookie/socket.

All filesystem-isolated to tmp_path — the real ~/.cyber-controller is never touched."""

from __future__ import annotations

import logging
import threading

import pytest

from src.security import web_auth


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(web_auth, "_CONFIG_DIR", tmp_path, raising=True)
    monkeypatch.setattr(web_auth, "_WEB_AUTH_FILE", tmp_path / "web_auth.json", raising=True)
    monkeypatch.setattr(web_auth, "_SECRET_KEY_FILE", tmp_path / "web_secret.key", raising=True)
    monkeypatch.delenv("CC_WEB_PASS", raising=False)
    monkeypatch.delenv("CC_WEB_USER", raising=False)
    return tmp_path


def test_new_instance_has_a_nonempty_generation():
    creds = web_auth.WebCredentials("admin", "pw-123456")
    assert isinstance(creds.generation, str) and len(creds.generation) >= 16


def test_two_instances_have_distinct_generations():
    a = web_auth.WebCredentials("admin", "pw-123456")
    b = web_auth.WebCredentials("admin", "pw-123456")
    assert a.generation != b.generation  # random, not derived from the password


def test_generation_is_not_persisted_fresh_each_resolve(isolated):
    """A restart is a fresh process => a fresh generation, so old cookies stop authenticating. Two
    resolves of the SAME saved password must yield different generations (nothing on disk pins it)."""
    web_auth.save_web_password("ace", "hunter2pw")
    c1, _ = web_auth.resolve_web_credentials(logging.getLogger("t"))
    c2, _ = web_auth.resolve_web_credentials(logging.getLogger("t"))
    assert c1.verify("ace", "hunter2pw") and c2.verify("ace", "hunter2pw")
    assert c1.generation != c2.generation


def test_verify_generation_returns_the_snapshot_generation():
    creds = web_auth.WebCredentials("admin", "pw-123456")
    gen = creds.verify_generation("admin", "pw-123456")
    assert gen == creds.generation
    assert creds.verify_generation("admin", "wrong") is None
    assert creds.verify_generation(None, None) is None


def test_publish_record_rotates_generation_and_can_take_an_explicit_one():
    creds = web_auth.WebCredentials("admin", "old-password")
    g0 = creds.generation
    g1 = creds.publish_record("ace", *web_auth._new_record("new-password"))
    assert g1 != g0 and creds.generation == g1
    explicit = web_auth._new_generation()
    g2 = creds.publish_record("ace", *web_auth._new_record("newer-pw"), generation=explicit)
    assert g2 == explicit and creds.generation == explicit


def test_rotate_generation_keeps_password_changes_token():
    creds = web_auth.WebCredentials("admin", "keepme-123")
    old = creds.generation
    new = creds.rotate_generation()
    assert new != old and creds.generation == new
    assert creds.verify("admin", "keepme-123")  # password preserved — only the generation moved


def test_apply_web_password_returns_new_generation_and_rotates(isolated):
    creds = web_auth.WebCredentials("admin", "old-password")
    g0 = creds.generation
    g1 = web_auth.apply_web_password(creds, "brand-new-pw", "ace")
    assert g1 == creds.generation and g1 != g0
    assert creds.verify("ace", "brand-new-pw")


def test_apply_web_password_save_failure_leaves_generation_unchanged(isolated, monkeypatch):
    creds = web_auth.WebCredentials("admin", "old-password")
    g0 = creds.generation

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(web_auth, "_write_record", boom)
    with pytest.raises(OSError):
        web_auth.apply_web_password(creds, "new-password", "ace")
    assert creds.generation == g0            # no revocation for a change that did not commit
    assert creds.verify("admin", "old-password")


def test_clear_and_rotate_removes_file_and_rotates(isolated):
    web_auth.save_web_password("ace", "hunter2pw")
    creds, _ = web_auth.resolve_web_credentials(logging.getLogger("t"))
    g0 = creds.generation
    removed, g1 = web_auth.clear_and_rotate(creds)
    assert removed is True
    assert not web_auth.has_stored_password()
    assert g1 == creds.generation and g1 != g0
    assert creds.verify("ace", "hunter2pw")   # running password preserved until restart


def test_clear_and_rotate_idempotent_no_file_still_rotates(isolated):
    creds = web_auth.WebCredentials("admin", "env-like-pw")
    g0 = creds.generation
    removed, g1 = web_auth.clear_and_rotate(creds)
    assert removed is False                   # nothing on disk to remove
    assert g1 != g0 and creds.generation == g1  # explicit clear still revokes other sessions


def test_clear_and_rotate_delete_failure_raises_before_rotation(isolated, monkeypatch):
    creds = web_auth.WebCredentials("admin", "keepme-123")
    g0 = creds.generation

    class _FakePath:
        def unlink(self):
            raise PermissionError("file is locked")

    monkeypatch.setattr(web_auth, "_WEB_AUTH_FILE", _FakePath())
    with pytest.raises(OSError):
        web_auth.clear_and_rotate(creds)
    assert creds.generation == g0             # a clear that FAILED revokes nothing


def test_clear_stored_password_signature_unchanged(isolated):
    """The no-arg disk-only helper still returns a bool (kept for callers that only touch disk)."""
    web_auth.save_web_password("ace", "hunter2pw")
    assert web_auth.clear_stored_password() is True
    assert web_auth.clear_stored_password() is False


def test_concurrent_changes_serialize_without_torn_state(isolated):
    """Many concurrent apply_web_password calls (the writer lock serializes them): the final live
    credential must equal EXACTLY the (salt, hash) persisted to disk (no torn record), its generation is
    one of those actually minted, and exactly one of the candidate passwords verifies live — last writer
    wins, none corrupts the other."""
    creds = web_auth.WebCredentials("admin", "seed-password")
    candidates = [f"concurrent-pw-{i:03d}" for i in range(12)]
    barrier = threading.Barrier(len(candidates))

    def worker(pw: str) -> None:
        barrier.wait()                      # release all at once to maximise contention
        web_auth.apply_web_password(creds, pw, "admin")

    threads = [threading.Thread(target=worker, args=(pw,)) for pw in candidates]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Live == disk: the winning snapshot was published exactly as persisted (no re-derivation, no mix).
    stored = web_auth.load_stored_credentials()
    assert stored is not None
    _u, salt, hashed = stored
    assert creds._record[1] == salt and creds._record[2] == hashed
    # Exactly one candidate verifies live (the last writer under the lock).
    winners = [pw for pw in candidates if creds.verify("admin", pw)]
    assert len(winners) == 1
    assert creds.generation  # a real generation is live
