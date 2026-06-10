"""Encrypted storage — passphrase-protected session data with authenticated AES-256-GCM.

Security policy (hardened):
    * AES-256-GCM (AEAD) is the ONLY cipher. There is NO unauthenticated fallback.
      If the `cryptography` package is unavailable, construction fails closed
      (raises) rather than silently degrading to a malleable XOR keystream.
    * Keys are derived with scrypt (memory-hard) when available, falling back to
      PBKDF2-HMAC-SHA256 (600k iterations) — both via `cryptography`/stdlib, never
      a hand-rolled primitive.
    * The 96-bit nonce is random per message; the 16-byte GCM tag authenticates
      both confidentiality and integrity, so any tampering is detected on decrypt.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import struct
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# AES-256-GCM is mandatory. Import at module load; SecureStorage fails closed if absent.
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    _HAS_CRYPTOGRAPHY = True
except ImportError:  # pragma: no cover - exercised only on a broken install
    AESGCM = None  # type: ignore[assignment]
    Scrypt = None  # type: ignore[assignment]
    _HAS_CRYPTOGRAPHY = False

_SALT_LEN = 16
_NONCE_LEN = 12  # 96-bit nonce for AES-GCM
_KEY_LEN = 32  # 256-bit key
_PBKDF2_ITERATIONS = 600_000
# scrypt work factors (N must be a power of two). N=2**15 r=8 p=1 ≈ 32 MiB — strong and
# fast enough for an interactive unlock; bump N if the threat model warrants it.
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_MAGIC = b"CYBC"  # File magic bytes
# Version 2 == scrypt KDF; version 1 (PBKDF2) is still accepted on decrypt for back-compat.
_VERSION = 2
_KDF_PBKDF2 = 1
_KDF_SCRYPT = 2


class CryptoUnavailableError(RuntimeError):
    """Raised when authenticated encryption cannot be provided (fail closed)."""


def _require_crypto() -> None:
    if not _HAS_CRYPTOGRAPHY:
        raise CryptoUnavailableError(
            "SecureStorage requires the `cryptography` package for authenticated "
            "AES-256-GCM encryption. Install it (`pip install cryptography`) — there "
            "is intentionally NO unauthenticated fallback."
        )


def _derive_key(passphrase: str, salt: bytes, kdf: int = _KDF_SCRYPT) -> bytes:
    """Derive a 256-bit key from *passphrase* and *salt*.

    Uses scrypt (memory-hard) for new vaults; PBKDF2-HMAC-SHA256 is supported so
    that version-1 vaults written before the scrypt upgrade still decrypt.
    """
    pw = passphrase.encode("utf-8")
    if kdf == _KDF_SCRYPT:
        _require_crypto()
        return Scrypt(salt=salt, length=_KEY_LEN, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P).derive(pw)
    # PBKDF2 path (stdlib) — only for reading legacy v1 blobs.
    return hashlib.pbkdf2_hmac("sha256", pw, salt, _PBKDF2_ITERATIONS, dklen=_KEY_LEN)


class SecureStorage:
    """Encrypt/decrypt arbitrary session data with a master passphrase.

    Storage format (binary):
        MAGIC (4 B) | VERSION (1 B) | KDF (1 B) | SALT (16 B) | NONCE (12 B) | CIPHERTEXT+TAG

    AES-256-GCM is the only cipher; the ciphertext includes a 16-byte authentication
    tag, so tampering is always detected on :meth:`decrypt` (it raises ``ValueError``).
    """

    def __init__(self, passphrase: str) -> None:
        _require_crypto()  # fail closed before any data is touched
        if not passphrase:
            raise ValueError("SecureStorage passphrase must not be empty")
        self._passphrase = passphrase
        log.info("SecureStorage: AES-256-GCM (scrypt KDF) initialised")

    # ── Encrypt / Decrypt ────────────────────────────────────────────

    def encrypt(self, data: dict[str, Any]) -> bytes:
        """Serialize *data* to JSON and encrypt it with AES-256-GCM.

        Returns:
            Raw ciphertext blob including header, KDF id, salt, and nonce.
        """
        _require_crypto()
        plaintext = json.dumps(data, separators=(",", ":")).encode("utf-8")
        salt = os.urandom(_SALT_LEN)
        nonce = os.urandom(_NONCE_LEN)
        key = _derive_key(self._passphrase, salt, kdf=_KDF_SCRYPT)
        ct = AESGCM(key).encrypt(nonce, plaintext, None)
        header = _MAGIC + struct.pack("BB", _VERSION, _KDF_SCRYPT)
        return header + salt + nonce + ct

    def decrypt(self, blob: bytes) -> dict[str, Any]:
        """Decrypt a blob produced by :meth:`encrypt`.

        Returns:
            The original dict.

        Raises:
            ValueError: On bad magic, wrong passphrase, or tampered/corrupted data
                (the GCM tag check fails closed).
        """
        _require_crypto()
        # v2 layout adds a KDF byte after VERSION; v1 had only VERSION then SALT.
        if len(blob) < len(_MAGIC) + 1 + _SALT_LEN + _NONCE_LEN:
            raise ValueError("Blob too short to be valid")
        if blob[:4] != _MAGIC:
            raise ValueError("Invalid file magic — not a CyberController vault")
        version = blob[4]
        if version == 1:
            kdf = _KDF_PBKDF2
            offset = 5
        elif version == 2:
            if len(blob) < len(_MAGIC) + 2 + _SALT_LEN + _NONCE_LEN:
                raise ValueError("Blob too short to be valid")
            kdf = blob[5]
            offset = 6
        else:
            raise ValueError(f"Unsupported vault version: {version}")

        salt = blob[offset:offset + _SALT_LEN]
        offset += _SALT_LEN
        nonce = blob[offset:offset + _NONCE_LEN]
        offset += _NONCE_LEN
        ct = blob[offset:]

        key = _derive_key(self._passphrase, salt, kdf=kdf)
        try:
            plaintext = AESGCM(key).decrypt(nonce, ct, None)
        except Exception as exc:  # InvalidTag etc. — never leak which
            raise ValueError("Decryption failed — wrong passphrase or tampered data") from exc

        try:
            return json.loads(plaintext.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Decrypted data is not valid JSON") from exc

    # ── File operations ──────────────────────────────────────────────

    def save(self, data: dict[str, Any], path: str | Path) -> None:
        """Encrypt and write *data* to *path* with owner-only (0600) permissions."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = self.encrypt(data)
        # Create with 0600 from the start (no world-readable window) where the OS supports it.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
        finally:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass  # best-effort on platforms without POSIX perms (Windows)
        log.info("Secure storage saved: %s", path)

    def load(self, path: str | Path) -> dict[str, Any]:
        """Read and decrypt the vault at *path*."""
        path = Path(path)
        blob = path.read_bytes()
        data = self.decrypt(blob)
        log.info("Secure storage loaded: %s", path)
        return data

    def wipe(self, path: str | Path) -> None:
        """Securely overwrite then delete the storage file.

        The file is overwritten with three passes (random, zeros, random) matching
        its original size before deletion.
        """
        path = Path(path)
        if not path.exists():
            return
        size = path.stat().st_size
        with path.open("r+b") as fh:
            for payload in (os.urandom(size), b"\x00" * size, os.urandom(size)):
                fh.seek(0)
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
        path.unlink()
        log.info("Secure storage wiped: %s", path)


# Backwards-compatible constant-time comparison helper for callers that previously
# leaned on this module for secret comparison (e.g. web auth). Kept here so security
# primitives live in one place.
def constant_time_equals(a: str, b: str) -> bool:
    """Constant-time string comparison (UTF-8) to resist timing side-channels."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
