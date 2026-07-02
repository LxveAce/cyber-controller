"""Web remote authentication & hardening helpers.

Centralises the security primitives the Flask/SocketIO remote needs:
    * a persistent, owner-only (0600) Flask secret key (sessions survive restarts);
    * credential resolution that NEVER ships a usable default — if CC_WEB_PASS is
      unset a strong random password is generated and printed once;
    * constant-time credential verification over a salted scrypt hash;
    * a small per-client in-memory rate limiter;
    * CSRF token generation/validation.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import sys
import threading
import time
from pathlib import Path

from src.security.win_acl import restrict_to_current_user, secure_dir

_CONFIG_DIR = Path.home() / ".cyber-controller"
_SECRET_KEY_FILE = _CONFIG_DIR / "web_secret.key"

# scrypt work factors for hashing the (already high-entropy) web password in memory.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1


def load_or_create_secret_key() -> bytes:
    """Return a stable 32-byte Flask secret key, persisted 0600 so signed sessions
    survive process restarts (the old code regenerated it every start, silently
    invalidating every session)."""
    # L-1: owner-only NTFS ACL on Windows (the 0600 below is a no-op there). A local user who
    # can read this key can forge authenticated session cookies for the web remote.
    secure_dir(_CONFIG_DIR)
    if _SECRET_KEY_FILE.exists():
        try:
            data = _SECRET_KEY_FILE.read_bytes()
            if len(data) >= 32:
                return data
        except OSError:
            pass
    key = os.urandom(32)
    fd = os.open(str(_SECRET_KEY_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
    finally:
        try:
            os.chmod(_SECRET_KEY_FILE, 0o600)
        except OSError:
            pass
    restrict_to_current_user(_SECRET_KEY_FILE)  # L-1: explicit owner-only ACL on Windows
    return key


class WebCredentials:
    """Holds a username and a salted scrypt hash of the password; verifies in
    constant time so neither field leaks via timing."""

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._salt = os.urandom(16)
        self._hash = self._derive(password)

    def _derive(self, password: str) -> bytes:
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=self._salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=32,
            maxmem=64 * 1024 * 1024,
        )

    def verify(self, username: str | None, password: str | None) -> bool:
        if username is None or password is None:
            return False
        try:
            u_ok = hmac.compare_digest(username.encode("utf-8"), self._username.encode("utf-8"))
            p_ok = hmac.compare_digest(self._derive(password), self._hash)
        except Exception:
            return False
        return u_ok and p_ok


def resolve_web_credentials(log: logging.Logger) -> tuple[WebCredentials, bool]:
    """Resolve web credentials from the environment, generating a strong one-time
    password when CC_WEB_PASS is unset. Returns (credentials, was_generated).

    There is intentionally NO usable default password (the old admin/cyber pair made
    every default deployment trivially accessible).
    """
    user = os.environ.get("CC_WEB_USER", "admin")
    pw = os.environ.get("CC_WEB_PASS")
    generated = False
    if not pw:
        pw = secrets.token_urlsafe(18)
        generated = True
        # Show the one-time credential on the interactive console (stderr) ONLY — never through the
        # logging framework. A file/syslog/aggregator handler would persist a live web-remote password
        # to disk, readable by anyone with log or backup access, defeating the "shown once" intent and
        # outliving the session. The log keeps only a non-secret notice.
        bar = "=" * 64
        print(bar, file=sys.stderr)
        print("CC_WEB_PASS not set — generated a ONE-TIME web remote password:", file=sys.stderr)
        print(f"      username: {user}", file=sys.stderr)
        print(f"      password: {pw}", file=sys.stderr)
        print("Set CC_WEB_USER / CC_WEB_PASS in the environment to pick your own.", file=sys.stderr)
        print(bar, file=sys.stderr)
        log.warning("CC_WEB_PASS not set — generated a one-time web remote password (shown on the console).")
    return WebCredentials(user, pw), generated


class RateLimiter:
    """Tiny fixed-window in-memory rate limiter keyed by an arbitrary string
    (typically the client IP). Thread-safe; suitable for a single-process server."""

    def __init__(self, max_events: int, window_seconds: float) -> None:
        self._max = max_events
        self._window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Record an event for *key*; return False if it exceeds the window budget."""
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._hits.get(key, []) if now - t < self._window]
            if len(recent) >= self._max:
                self._hits[key] = recent
                return False
            recent.append(now)
            self._hits[key] = recent
            return True


def new_csrf_token() -> str:
    """Return a fresh, unguessable CSRF/connection token."""
    return secrets.token_urlsafe(32)


def csrf_valid(expected: str | None, provided: str | None) -> bool:
    """Constant-time CSRF token comparison."""
    if not expected or not provided:
        return False
    return hmac.compare_digest(str(expected), str(provided))
