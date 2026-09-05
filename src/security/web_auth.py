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
import json
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
#: Persisted web password the user set from the UI: username + a salted scrypt hash (NEVER plaintext),
#: owner-only (0600). Its presence means "the user picked a password here" and it wins over the env var
#: and the one-time generated password so the in-app change actually takes effect.
_WEB_AUTH_FILE = _CONFIG_DIR / "web_auth.json"

#: Minimum length for a user-chosen web password. Short enough not to nag, long enough to matter on a
#: LAN-exposed remote. The generated one-time password is far longer.
MIN_PASSWORD_LEN = 8

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

    def __init__(self, username: str, password: str | None = None, *,
                 salt: bytes | None = None, hashed: bytes | None = None) -> None:
        self._username = username
        if salt is not None and hashed is not None:
            # Reconstruct from a persisted (salt, hash) — no plaintext needed or kept.
            self._salt = salt
            self._hash = hashed
        else:
            self._salt = os.urandom(16)
            self._hash = self._derive(password or "")
        # The plaintext of a GENERATED one-time password, kept in RAM only so an already-authenticated
        # operator can read it back in the UI to reach the server from a phone / another PC (a windowed
        # build swallows the stderr print). None for a user-set password — the owner already knows that
        # one, and we never store or reveal a secret the user chose. Never written to disk or logs.
        self.generated_password: str | None = None
        # How the ACTIVE password was set, so the UI can explain it plainly instead of pointing at an
        # env var: "generated" (one-time), "env" (CC_WEB_PASS), or "saved" (set in the app).
        self.source: str = "env"

    @property
    def username(self) -> str:
        return self._username

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

    def set_password(self, new_password: str, username: str | None = None) -> None:
        """Replace the in-memory password (fresh salt + hash) and optionally the username. Clears any
        one-time generated password — after the user picks their own, there is no one-time to reveal.
        Persisting it to disk is :func:`save_web_password`'s job, kept separate so the class stays pure."""
        self.publish_record(username or self._username, *_new_record(new_password))

    def publish_record(self, username: str, salt: bytes, hashed: bytes) -> None:
        """Adopt a PRE-DERIVED (salt, hash) — no re-derivation. This is what makes the in-memory publish
        share the exact snapshot that was persisted, so nothing fallible runs after the disk write (the
        old set_password re-derived here, which could fail and leave disk and memory disagreeing)."""
        self._username = username
        self._salt = salt
        self._hash = hashed
        self.generated_password = None
        self.source = "saved"

    def verify(self, username: str | None, password: str | None) -> bool:
        if username is None or password is None:
            return False
        try:
            u_ok = hmac.compare_digest(username.encode("utf-8"), self._username.encode("utf-8"))
            p_ok = hmac.compare_digest(self._derive(password), self._hash)
        except Exception:
            return False
        return u_ok and p_ok


# Serialize credential mutations so two concurrent password changes can't interleave a half-written
# file with a published in-memory state (the web server runs threaded).
_cred_lock = threading.Lock()

_SALT_LEN = 16
_HASH_LEN = 32


def load_stored_credentials() -> tuple[str, bytes, bytes] | None:
    """Load a UI-set password from ``web_auth.json`` as ``(username, salt, hash)``, or None if there is
    no saved password OR the record is structurally invalid (bad JSON, wrong types, empty/short salt or
    hash, or KDF params this build can't reproduce). We never fail OPEN on a bad file — an invalid
    record is ignored and CC falls through to the env / one-time path, never to blank credentials."""
    try:
        raw = _WEB_AUTH_FILE.read_text("utf-8")
    except (OSError, ValueError):
        # ValueError covers UnicodeDecodeError — a binary/garbled file is a corrupt record, not a crash.
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    username = data.get("username")
    salt_hex = data.get("salt")
    hash_hex = data.get("hash")
    if not isinstance(username, str) or not username:
        return None
    if not isinstance(salt_hex, str) or not isinstance(hash_hex, str):
        return None
    try:
        salt = bytes.fromhex(salt_hex)
        hashed = bytes.fromhex(hash_hex)
    except ValueError:
        return None
    if len(salt) != _SALT_LEN or len(hashed) != _HASH_LEN:
        return None
    # The stored hash was produced with these scrypt params; _derive() reproduces it only with the same
    # ones. If a record carries different params, we can't verify against it — reject rather than accept
    # a credential that can never authenticate.
    for key, expected in (("n", _SCRYPT_N), ("r", _SCRYPT_R), ("p", _SCRYPT_P)):
        if key in data and data[key] != expected:
            return None
    return (username, salt, hashed)


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                          p=_SCRYPT_P, dklen=32, maxmem=64 * 1024 * 1024)


def _new_record(password: str) -> tuple[bytes, bytes]:
    """Derive ONE (salt, hash) snapshot for a password. Deriving once — instead of separately for the
    disk write and the in-memory publish — is what lets both share the exact same credential (R04)."""
    salt = os.urandom(16)
    return salt, _scrypt(password, salt)


def _write_record(username: str, salt: bytes, hashed: bytes) -> None:
    """Atomically persist a pre-derived credential record (temp file → fsync → ``os.replace``), owner-only
    (0600). Raises on any failure, cleaning up the temp file — never leaves a torn/partial file."""
    secure_dir(_CONFIG_DIR)
    payload = json.dumps({
        "v": 1, "username": username, "salt": salt.hex(), "hash": hashed.hex(),
        "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P,
    })
    tmp = _WEB_AUTH_FILE.with_name(_WEB_AUTH_FILE.name + ".tmp-" + os.urandom(4).hex())
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        restrict_to_current_user(tmp)
        os.replace(str(tmp), str(_WEB_AUTH_FILE))  # atomic swap into place
    except BaseException:
        try:
            os.remove(str(tmp))
        except OSError:
            pass
        raise
    restrict_to_current_user(_WEB_AUTH_FILE)  # owner-only NTFS ACL on Windows


def save_web_password(username: str, password: str) -> None:
    """Persist a user-chosen web password (username + fresh salt + scrypt hash, NEVER plaintext) to
    ``web_auth.json``, owner-only (0600), atomically. Raises on any failure — the caller must publish the
    new in-memory password only after this returns."""
    _write_record(username, *_new_record(password))


def apply_web_password(creds: "WebCredentials", new_password: str, username: str) -> None:
    """Persist THEN publish, under a lock, from ONE credential snapshot: derive (salt, hash) once, persist
    it, then publish that exact snapshot into the live credentials. A failed save leaves the running
    password unchanged, and nothing fallible (no second derivation) runs after the disk write — so disk
    and memory can never end up disagreeing."""
    with _cred_lock:
        salt, hashed = _new_record(new_password)          # derive ONCE
        _write_record(username, salt, hashed)             # persist first; raises -> nothing published
        creds.publish_record(username, salt, hashed)      # publish the SAME snapshot, no re-derivation


def clear_stored_password() -> bool:
    """Delete the saved password so CC reverts to CC_WEB_PASS / a one-time password on next start.
    Returns True if a file was removed, False if none existed (both are success). **Raises OSError on a
    real failure** (permission/I/O) so the caller can report it truthfully instead of claiming success."""
    with _cred_lock:
        try:
            _WEB_AUTH_FILE.unlink()
            return True
        except FileNotFoundError:
            return False  # already absent — idempotent success, nothing removed
        # PermissionError / other OSError propagate to the caller


def has_stored_password() -> bool:
    return _WEB_AUTH_FILE.exists()


def resolve_web_credentials(log: logging.Logger) -> tuple[WebCredentials, bool]:
    """Resolve web credentials. Priority: a password the user SAVED in the app > the ``CC_WEB_PASS``
    env var > a strong one-time password generated on the spot. Returns (credentials, was_generated).

    There is intentionally NO usable default password (the old admin/cyber pair made every default
    deployment trivially accessible). The saved-in-app password wins so that changing it from the UI
    actually takes effect without touching environment variables — the thing users found confusing.
    """
    stored = load_stored_credentials()
    if stored is None and _WEB_AUTH_FILE.exists():
        log.warning("Saved web password file is present but invalid (bad schema/KDF) — ignoring it and "
                    "using the env / one-time password. Set a new one in Settings -> Remote Access.")
    if stored is not None:
        username, salt, hashed = stored
        result = WebCredentials(username, salt=salt, hashed=hashed)
        result.source = "saved"
        log.info("Web remote using the password set in the app (Settings -> Remote Access).")
        return result, False

    user = os.environ.get("CC_WEB_USER", "admin")
    pw = os.environ.get("CC_WEB_PASS")
    if pw:
        result = WebCredentials(user, pw)
        result.source = "env"
        return result, False

    # Nothing saved, no env var: mint a one-time password so the remote is never open, and tell the
    # user (console + the in-app Remote Access card) how to set their own.
    pw = secrets.token_urlsafe(18)
    # Show the one-time credential on the interactive console (stderr) ONLY — never through the logging
    # framework. A file/syslog/aggregator handler would persist a live web-remote password to disk,
    # readable by anyone with log or backup access, defeating the "shown once" intent. The log keeps
    # only a non-secret notice.
    bar = "=" * 64
    print(bar, file=sys.stderr)
    print("No web password set yet — generated a ONE-TIME web remote password:", file=sys.stderr)
    print(f"      username: {user}", file=sys.stderr)
    print(f"      password: {pw}", file=sys.stderr)
    print("Log in with it, then set your own in Settings -> Remote Access (no env vars needed).",
          file=sys.stderr)
    print(bar, file=sys.stderr)
    log.warning("No web password set — generated a one-time web remote password (shown on the console).")
    result = WebCredentials(user, pw)
    result.source = "generated"
    result.generated_password = pw  # RAM-only, for the authenticated Remote-Access reveal in the UI
    return result, True


class RateLimiter:
    """Tiny fixed-window in-memory rate limiter keyed by an arbitrary string
    (typically the client IP). Thread-safe; suitable for a single-process server."""

    def __init__(self, max_events: int, window_seconds: float) -> None:
        self._max = max_events
        self._window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        # allow() only touches the key it is called with, so a one-shot client's entry (every distinct
        # source IP the server ever sees) would otherwise linger forever — an unbounded leak in shared
        # server state. A periodic full sweep drops keys whose newest event has aged out, bounding
        # memory to currently-active clients. Amortized O(n) at most once per window.
        self._last_sweep = 0.0

    def _sweep(self, now: float) -> None:
        """Drop keys whose most-recent event has fully aged out of the window."""
        self._hits = {
            k: ts for k, ts in self._hits.items() if ts and now - ts[-1] < self._window
        }
        self._last_sweep = now

    def allow(self, key: str) -> bool:
        """Record an event for *key*; return False if it exceeds the window budget."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_sweep >= self._window:
                self._sweep(now)
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
    try:
        return hmac.compare_digest(str(expected), str(provided))
    except TypeError:
        # compare_digest raises TypeError when a str holds non-ASCII characters. A client-supplied
        # token (header / WS handshake) with a non-ASCII byte is simply invalid — fail closed to a
        # clean 403 rather than letting the TypeError escape as an uncaught HTTP 500. No timing
        # oracle: our tokens are ASCII url-safe base64, so a non-ASCII token is structurally wrong
        # before any comparison of the real secret.
        return False
