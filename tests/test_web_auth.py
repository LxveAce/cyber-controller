"""Tests for ``src.security.web_auth`` (web-remote auth primitives).

Covered (pure stdlib — NO flask import needed):
    * ``WebCredentials.verify`` is True only for the exact user+pass;
    * ``RateLimiter(2, 60)`` allows 2 events then blocks the 3rd;
    * ``csrf_valid`` is True for matching tokens, False for mismatch/None;
    * ``load_or_create_secret_key`` returns >= 32 bytes (redirected to tmp).

``web_auth`` itself has no flask dependency, but it is imported behind
``importorskip`` for consistency with the rest of the suite.
"""

from __future__ import annotations

import pytest

web_auth = pytest.importorskip("src.security.web_auth")


# ── WebCredentials.verify ────────────────────────────────────────────

def test_credentials_verify_correct() -> None:
    creds = web_auth.WebCredentials("admin", "secret")
    assert creds.verify("admin", "secret") is True


@pytest.mark.parametrize(
    "user, pw",
    [
        ("admin", "wrong"),     # wrong password
        ("root", "secret"),     # wrong username
        ("root", "wrong"),      # both wrong
        (None, "secret"),       # missing username
        ("admin", None),        # missing password
        ("", ""),               # empty
    ],
)
def test_credentials_verify_rejects(user, pw) -> None:
    creds = web_auth.WebCredentials("admin", "secret")
    assert creds.verify(user, pw) is False


# ── RateLimiter ──────────────────────────────────────────────────────

def test_rate_limiter_allows_then_blocks() -> None:
    rl = web_auth.RateLimiter(2, 60)
    assert rl.allow("1.2.3.4") is True   # 1st
    assert rl.allow("1.2.3.4") is True   # 2nd
    assert rl.allow("1.2.3.4") is False  # 3rd -> over budget


def test_rate_limiter_keys_are_independent() -> None:
    rl = web_auth.RateLimiter(1, 60)
    assert rl.allow("a") is True
    assert rl.allow("a") is False
    # A different key has its own budget.
    assert rl.allow("b") is True


# ── csrf_valid ───────────────────────────────────────────────────────

def test_csrf_valid_matching() -> None:
    tok = web_auth.new_csrf_token()
    assert web_auth.csrf_valid(tok, tok) is True


@pytest.mark.parametrize(
    "expected, provided",
    [
        ("token-value", "x"),
        ("token-value", None),
        (None, "token-value"),
        (None, None),
        ("", ""),
    ],
)
def test_csrf_valid_rejects(expected, provided) -> None:
    assert web_auth.csrf_valid(expected, provided) is False


# ── load_or_create_secret_key ────────────────────────────────────────

def test_secret_key_at_least_32_bytes(tmp_path, monkeypatch) -> None:
    # Redirect the persisted key location to a tmp dir so the real
    # ~/.cyber-controller is never touched and the test is deterministic.
    key_file = tmp_path / "web_secret.key"
    monkeypatch.setattr(web_auth, "_CONFIG_DIR", tmp_path, raising=True)
    monkeypatch.setattr(web_auth, "_SECRET_KEY_FILE", key_file, raising=True)

    key = web_auth.load_or_create_secret_key()
    assert isinstance(key, (bytes, bytearray))
    assert len(key) >= 32

    # A second call returns the SAME persisted key (sessions survive restart).
    assert web_auth.load_or_create_secret_key() == key
