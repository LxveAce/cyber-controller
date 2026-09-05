"""Persisted, UI-settable web password: save_web_password / clear_stored_password + the resolve
priority (saved-in-app > CC_WEB_PASS env > one-time generated). This is the redesign that lets a user
set the web login password from the app instead of an environment variable.

All filesystem-isolated to tmp_path — the real ~/.cyber-controller is never touched."""

from __future__ import annotations

import json
import logging
import os

import pytest

from src.security import web_auth


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point web_auth's config paths at a temp dir and clear the env credentials."""
    monkeypatch.setattr(web_auth, "_CONFIG_DIR", tmp_path, raising=True)
    monkeypatch.setattr(web_auth, "_WEB_AUTH_FILE", tmp_path / "web_auth.json", raising=True)
    monkeypatch.setattr(web_auth, "_SECRET_KEY_FILE", tmp_path / "web_secret.key", raising=True)
    monkeypatch.delenv("CC_WEB_PASS", raising=False)
    monkeypatch.delenv("CC_WEB_USER", raising=False)
    return tmp_path


def test_no_config_generates_one_time(isolated):
    creds, generated = web_auth.resolve_web_credentials(logging.getLogger("t"))
    assert generated is True
    assert creds.source == "generated"
    assert creds.generated_password  # revealed in RAM for the authenticated UI
    assert creds.verify(creds.username, creds.generated_password)


def test_saved_password_wins_and_persists(isolated):
    web_auth.save_web_password("ace", "hunter2pw")
    assert web_auth.has_stored_password()
    creds, generated = web_auth.resolve_web_credentials(logging.getLogger("t"))
    assert generated is False
    assert creds.source == "saved"
    assert creds.username == "ace"
    assert creds.verify("ace", "hunter2pw")
    assert not creds.verify("ace", "wrong-password")
    assert creds.generated_password is None  # nothing to reveal — the user knows their own


def test_saved_password_beats_env(isolated, monkeypatch):
    monkeypatch.setenv("CC_WEB_PASS", "env-password")
    monkeypatch.setenv("CC_WEB_USER", "envuser")
    web_auth.save_web_password("ace", "saved-password")
    creds, _ = web_auth.resolve_web_credentials(logging.getLogger("t"))
    assert creds.source == "saved"
    assert creds.verify("ace", "saved-password")
    assert not creds.verify("envuser", "env-password")


def test_env_used_when_no_saved(isolated, monkeypatch):
    monkeypatch.setenv("CC_WEB_PASS", "env-password")
    creds, generated = web_auth.resolve_web_credentials(logging.getLogger("t"))
    assert generated is False
    assert creds.source == "env"
    assert creds.verify("admin", "env-password")


def test_clear_reverts_to_generated(isolated):
    web_auth.save_web_password("ace", "hunter2pw")
    assert web_auth.clear_stored_password() is True
    assert not web_auth.has_stored_password()
    creds, generated = web_auth.resolve_web_credentials(logging.getLogger("t"))
    assert generated is True
    assert creds.source == "generated"


def test_stored_file_never_contains_plaintext(isolated):
    web_auth.save_web_password("ace", "SuperSecretPlain")
    raw = (isolated / "web_auth.json").read_text("utf-8")
    assert "SuperSecretPlain" not in raw
    assert "hash" in raw and "salt" in raw


def test_set_password_updates_live_credentials(isolated):
    creds = web_auth.WebCredentials("admin", "old-password")
    assert creds.verify("admin", "old-password")
    creds.set_password("brand-new-password", "ace")
    assert creds.source == "saved"
    assert creds.username == "ace"
    assert creds.verify("ace", "brand-new-password")
    assert not creds.verify("admin", "old-password")
    assert creds.generated_password is None


def test_corrupt_store_falls_through_not_open(isolated, monkeypatch):
    (isolated / "web_auth.json").write_text("{ not valid json", "utf-8")
    monkeypatch.setenv("CC_WEB_PASS", "env-password")
    creds, _ = web_auth.resolve_web_credentials(logging.getLogger("t"))
    # A corrupt saved file must not fail open — it falls through to the env password, not a blank one.
    assert creds.source == "env"
    assert creds.verify("admin", "env-password")


# -- D01: persist-then-publish (a failed save must not change live creds) --

def test_apply_web_password_save_failure_leaves_live_creds(isolated, monkeypatch):
    creds = web_auth.WebCredentials("admin", "old-password")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(web_auth, "save_web_password", boom)
    with pytest.raises(OSError):
        web_auth.apply_web_password(creds, "new-password", "ace")
    assert creds.verify("admin", "old-password")       # live password unchanged
    assert not creds.verify("ace", "new-password")


def test_apply_web_password_success_persists_and_publishes(isolated):
    creds = web_auth.WebCredentials("admin", "old-password")
    web_auth.apply_web_password(creds, "brand-new-pw", "ace")
    assert creds.verify("ace", "brand-new-pw")          # live updated
    reloaded, _ = web_auth.resolve_web_credentials(logging.getLogger("t"))
    assert reloaded.verify("ace", "brand-new-pw")       # and persisted


def test_save_failure_leaves_old_file_intact(isolated, monkeypatch):
    web_auth.save_web_password("ace", "first-password")
    before = (isolated / "web_auth.json").read_text("utf-8")

    def boom(src, dst):
        raise OSError("atomic replace failed")

    monkeypatch.setattr(web_auth.os, "replace", boom)
    with pytest.raises(OSError):
        web_auth.save_web_password("ace", "second-password")
    assert (isolated / "web_auth.json").read_text("utf-8") == before   # torn write can't happen
    assert not any(".tmp-" in n for n in os.listdir(str(isolated)))     # temp file cleaned up


# -- D02: reset reports the truth ------------------------------------

def test_clear_absent_is_idempotent_false(isolated):
    assert web_auth.has_stored_password() is False
    assert web_auth.clear_stored_password() is False   # nothing to remove, but not an error


def test_clear_removed_returns_true(isolated):
    web_auth.save_web_password("ace", "hunter2pw")
    assert web_auth.clear_stored_password() is True
    assert web_auth.has_stored_password() is False


def test_clear_raises_on_permission_failure(isolated, monkeypatch):
    class _FakePath:
        def unlink(self):
            raise PermissionError("file is locked")

        def exists(self):
            return True

    monkeypatch.setattr(web_auth, "_WEB_AUTH_FILE", _FakePath())
    with pytest.raises(OSError):
        web_auth.clear_stored_password()               # must NOT swallow it as success


# -- D03: structurally invalid records are rejected ------------------

def test_load_rejects_invalid_records(isolated):
    f = isolated / "web_auth.json"
    good_salt, good_hash = "00" * 16, "00" * 32
    bad_records = [
        {"username": "ace", "salt": "", "hash": ""},                       # empty
        {"username": "ace", "salt": "00", "hash": good_hash},              # short salt
        {"username": "ace", "salt": good_salt, "hash": "00"},             # short hash
        {"username": 123, "salt": good_salt, "hash": good_hash},          # wrong username type
        {"username": "", "salt": good_salt, "hash": good_hash},           # empty username
        {"username": "ace", "salt": good_salt, "hash": good_hash, "n": 2},  # unsupported KDF
        {"username": "ace", "salt": "zz" * 16, "hash": good_hash},        # non-hex
        ["not", "a", "dict"],                                              # wrong top-level type
    ]
    for rec in bad_records:
        f.write_text(json.dumps(rec), "utf-8")
        assert web_auth.load_stored_credentials() is None, rec


def test_load_accepts_valid_record(isolated):
    web_auth.save_web_password("ace", "hunter2pw")
    got = web_auth.load_stored_credentials()
    assert got is not None
    assert got[0] == "ace" and len(got[1]) == 16 and len(got[2]) == 32
