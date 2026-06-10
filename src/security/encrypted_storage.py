"""Encrypted storage — passphrase-protected session data with AES-256-GCM or XOR fallback."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import struct
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Try to use the `cryptography` library for AES-256-GCM.
# Fall back to a PBKDF2 + XOR stream cipher when it is unavailable.
_HAS_CRYPTOGRAPHY = False
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    _HAS_CRYPTOGRAPHY = True
except ImportError:
    pass

_SALT_LEN = 16
_NONCE_LEN = 12  # 96-bit nonce for AES-GCM
_KEY_LEN = 32  # 256-bit key
_PBKDF2_ITERATIONS = 600_000
_MAGIC = b"CYBC"  # File magic bytes
_VERSION = 1


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """PBKDF2-HMAC-SHA256 key derivation."""
    return hashlib.pbkdf2_hmac(
        "sha256",
        passphrase.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
        dklen=_KEY_LEN,
    )


# ── XOR stream fallback ─────────────────────────────────────────────

def _xor_keystream(key: bytes, length: int) -> bytes:
    """Generate a deterministic keystream from *key* using repeated SHA-256 hashing."""
    stream = bytearray()
    block = key
    while len(stream) < length:
        block = hashlib.sha256(block).digest()
        stream.extend(block)
    return bytes(stream[:length])


def _xor_encrypt(plaintext: bytes, key: bytes, nonce: bytes) -> bytes:
    """XOR stream cipher — NOT authenticated; used only as a last resort."""
    seed = hashlib.sha256(key + nonce).digest()
    ks = _xor_keystream(seed, len(plaintext))
    return bytes(a ^ b for a, b in zip(plaintext, ks))


_xor_decrypt = _xor_encrypt  # Symmetric


# ── SecureStorage ────────────────────────────────────────────────────

class SecureStorage:
    """Encrypt/decrypt arbitrary session data with a master passphrase.

    Storage format (binary):
        MAGIC (4 B) | VERSION (1 B) | SALT (16 B) | NONCE (12 B) | CIPHERTEXT (variable)

    When the ``cryptography`` package is installed, AES-256-GCM is used and
    the ciphertext includes a 16-byte authentication tag.  Otherwise, a
    PBKDF2 + XOR stream cipher is used (functional but not authenticated).
    """

    def __init__(self, passphrase: str) -> None:
        self._passphrase = passphrase
        if _HAS_CRYPTOGRAPHY:
            log.info("SecureStorage: using AES-256-GCM (cryptography library)")
        else:
            log.warning(
                "SecureStorage: `cryptography` not installed — "
                "falling back to PBKDF2+XOR (no authentication tag)"
            )

    # ── Encrypt / Decrypt ────────────────────────────────────────────

    def encrypt(self, data: dict[str, Any]) -> bytes:
        """Serialize *data* to JSON and encrypt it.

        Returns:
            Raw ciphertext blob including header, salt, nonce.
        """
        plaintext = json.dumps(data, separators=(",", ":")).encode("utf-8")
        salt = os.urandom(_SALT_LEN)
        nonce = os.urandom(_NONCE_LEN)
        key = _derive_key(self._passphrase, salt)

        if _HAS_CRYPTOGRAPHY:
            ct = AESGCM(key).encrypt(nonce, plaintext, None)
        else:
            ct = _xor_encrypt(plaintext, key, nonce)

        header = _MAGIC + struct.pack("B", _VERSION)
        return header + salt + nonce + ct

    def decrypt(self, blob: bytes) -> dict[str, Any]:
        """Decrypt a blob produced by :meth:`encrypt`.

        Returns:
            The original dict.

        Raises:
            ValueError: On bad magic, wrong passphrase, or corrupted data.
        """
        if len(blob) < len(_MAGIC) + 1 + _SALT_LEN + _NONCE_LEN:
            raise ValueError("Blob too short to be valid")
        if blob[:4] != _MAGIC:
            raise ValueError("Invalid file magic — not a CyberController vault")
        version = blob[4]
        if version != _VERSION:
            raise ValueError(f"Unsupported vault version: {version}")

        offset = 5
        salt = blob[offset : offset + _SALT_LEN]
        offset += _SALT_LEN
        nonce = blob[offset : offset + _NONCE_LEN]
        offset += _NONCE_LEN
        ct = blob[offset:]

        key = _derive_key(self._passphrase, salt)

        if _HAS_CRYPTOGRAPHY:
            try:
                plaintext = AESGCM(key).decrypt(nonce, ct, None)
            except Exception as exc:
                raise ValueError("Decryption failed — wrong passphrase or corrupted data") from exc
        else:
            plaintext = _xor_decrypt(ct, key, nonce)

        try:
            return json.loads(plaintext.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Decrypted data is not valid JSON — wrong passphrase?") from exc

    # ── File operations ──────────────────────────────────────────────

    def save(self, data: dict[str, Any], path: str | Path) -> None:
        """Encrypt and write *data* to *path*."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.encrypt(data))
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

        The file is overwritten with random bytes matching its original
        size before deletion.
        """
        path = Path(path)
        if not path.exists():
            return
        size = path.stat().st_size
        # Three-pass overwrite: random, zeros, random
        with path.open("r+b") as fh:
            fh.write(os.urandom(size))
            fh.flush()
            os.fsync(fh.fileno())
            fh.seek(0)
            fh.write(b"\x00" * size)
            fh.flush()
            os.fsync(fh.fileno())
            fh.seek(0)
            fh.write(os.urandom(size))
            fh.flush()
            os.fsync(fh.fileno())
        path.unlink()
        log.info("Secure storage wiped: %s", path)
