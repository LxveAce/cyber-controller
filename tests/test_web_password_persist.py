"""Persisted, UI-settable web password: save_web_password / clear_stored_password + the resolve
priority (saved-in-app > CC_WEB_PASS env > one-time generated). This is the redesign that lets a user
set the web login password from the app instead of an environment variable.

All filesystem-isolated to tmp_path — the real ~/.cyber-controller is never touched."""

from __future__ import annotations

import logging

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
