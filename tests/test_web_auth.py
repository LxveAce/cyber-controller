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


def test_rate_limiter_evicts_stale_keys(monkeypatch) -> None:
    """Every distinct source IP the web remote ever sees must NOT leave a permanent entry: once a
    client's events fully age out of the window its key has to be dropped, bounding _hits to
    currently-active clients. Without the periodic sweep the dict grows unbounded (one entry per IP
    ever seen). Uses a fake monotonic clock so the test is deterministic and fast."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(web_auth.time, "monotonic", lambda: clock["t"])

    rl = web_auth.RateLimiter(max_events=5, window_seconds=10.0)
    # 50 one-shot clients within the window — each leaves a key.
    for i in range(50):
        assert rl.allow(f"10.0.0.{i}") is True
    assert len(rl._hits) == 50

    # Advance well past the window so every seeded key is stale, then one new request triggers the
    # sweep. Only the currently-active client should remain — not 51 keys.
    clock["t"] += 100.0
    assert rl.allow("192.168.1.1") is True
    assert len(rl._hits) == 1


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


@pytest.mark.parametrize("provided", ["café", "tokén", "❤", "abc\x80def"])
def test_csrf_valid_rejects_non_ascii_without_raising(provided) -> None:
    """A client-supplied token with a non-ASCII char must resolve to a clean False (→ 403), NOT an
    uncaught TypeError from hmac.compare_digest (→ HTTP 500). The token is client-controlled (a
    request header / WS handshake), so a garbled or hostile value fails closed, not crash-500s."""
    expected = web_auth.new_csrf_token()  # ASCII url-safe base64
    assert web_auth.csrf_valid(expected, provided) is False
    # And symmetric: a non-ASCII *expected* (defensive) also fails closed rather than raising.
    assert web_auth.csrf_valid(provided, expected) is False


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


def test_generated_web_password_goes_to_console_not_logs(monkeypatch, capsys) -> None:
    """A generated one-time web password must be shown on the console (stderr) but must NOT be emitted
    through the logging framework — log handlers would persist a live credential to disk."""
    import logging
    import re

    monkeypatch.delenv("CC_WEB_PASS", raising=False)
    monkeypatch.delenv("CC_WEB_USER", raising=False)

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())

    lg = logging.getLogger("test_web_auth_generated")
    lg.setLevel(logging.DEBUG)
    lg.addHandler(_Capture())

    _creds, generated = web_auth.resolve_web_credentials(lg)
    assert generated

    err = capsys.readouterr().err
    # Anchor to the indented "      password: <pw>" value line. A bare `password:\s*(\S+)` instead matches
    # the earlier header line ("...web remote password:") and \s* eats the newline+indent, capturing the
    # literal "username:" token rather than the secret — leaving the leak assertion below blind.
    m = re.search(r"(?m)^\s+password:\s*(\S+)", err)
    assert m, f"generated password not shown on the console: {err!r}"
    shown_pw = m.group(1)
    assert ":" not in shown_pw, f"captured a label, not the password: {shown_pw!r}"

    # the live password must appear ONLY on the console, never in any log record
    assert all(shown_pw not in rec for rec in records), "generated web password leaked into logs"
    assert any("shown on the console" in rec for rec in records)  # only a non-secret notice is logged
