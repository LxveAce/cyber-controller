"""Gate-keyed encrypted vault — sensitive data stays AES-256-GCM encrypted at rest and is
inaccessible until an access-gate factor (admin password and/or physical USB key) is provided.

Design (LUKS-style keyslots, defense-in-depth behind the launch gate):
  * A random 32-byte Data Encryption Key (DEK) encrypts the vault data file (AES-256-GCM).
  * The DEK is *key-wrapped* under each configured factor: KEK = scrypt(factor_secret, salt);
    slot = AES-256-GCM(KEK).encrypt(DEK). Unlocking ANY configured factor unwraps the DEK.
  * Neither the factor secrets nor the DEK are ever written to disk in the clear. Without a factor
    the DEK is unrecoverable, so `vault.enc` stays opaque ciphertext (the access requirement).
  * Adding a new factor (keyslot) requires an EXISTING factor to first unwrap the DEK — so you can
    never silently clobber/relock a vault you cannot currently open.

This is the at-rest counterpart to the startup access gate: even if the launch gate is bypassed or
its config is deleted, the data in the vault remains encrypted and unreadable without the password/key.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.security.win_acl import restrict_to_current_user, secure_dir

_DEFAULT_DIR = Path.home() / ".cyber-controller"
_HDR_NAME = "vault.hdr.json"   # keyslots + salt + check token (no secrets, no DEK in the clear)
_DATA_NAME = "vault.enc"       # AES-256-GCM(DEK) over the JSON payload
_CHECK_TOKEN = b"cyber-controller-vault-v1"

_N, _R, _P, _DKLEN = 2 ** 15, 8, 1, 32
_MAXMEM = 128 * 1024 * 1024


class NeedExistingFactor(Exception):
    """Raised when adding a keyslot but no currently-available factor can unwrap the DEK."""


def _dir() -> Path:
    return Path(os.environ.get("CC_VAULT_DIR") or _DEFAULT_DIR)


def _hdr_path() -> Path:
    return _dir() / _HDR_NAME


def _data_path() -> Path:
    return _dir() / _DATA_NAME


def _scrypt(secret: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(secret, salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN, maxmem=_MAXMEM)


def _wrap(dek: bytes, kek: bytes) -> dict:
    nonce = os.urandom(12)
    return {"nonce": nonce.hex(), "ct": AESGCM(kek).encrypt(nonce, dek, None).hex()}


def _unwrap(slot: dict, kek: bytes) -> Optional[bytes]:
    try:
        return AESGCM(kek).decrypt(bytes.fromhex(slot["nonce"]), bytes.fromhex(slot["ct"]), None)
    except Exception:
        return None


# ── header / status ──────────────────────────────────────────────────

def exists() -> bool:
    """True if a vault (header or data) is present — used by the gate's tamper fail-closed check."""
    return _hdr_path().exists() or _data_path().exists()


def is_provisioned() -> bool:
    return _hdr_path().exists()


def factors() -> list[str]:
    return list(_load_hdr().get("slots", {})) if is_provisioned() else []


def _load_hdr() -> dict:
    p = _hdr_path()
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _save_hdr(hdr: dict) -> None:
    p = _hdr_path()
    secure_dir(p.parent)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(hdr, fh, indent=2)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    restrict_to_current_user(p)


def _dek_from(hdr: dict, available: dict) -> Optional[bytes]:
    salt = bytes.fromhex(hdr["salt"])
    slots = hdr.get("slots", {})
    chk = hdr.get("check")
    for name, secret in available.items():
        slot = slots.get(name)
        if not slot or secret is None:
            continue
        dek = _unwrap(slot, _scrypt(secret, salt))
        if dek is None:
            continue
        if chk and not _check_ok(dek, chk):
            continue
        return dek
    return None


def _check_ok(dek: bytes, chk: dict) -> bool:
    try:
        pt = AESGCM(dek).decrypt(bytes.fromhex(chk["nonce"]), bytes.fromhex(chk["ct"]), None)
        return pt == _CHECK_TOKEN
    except Exception:
        return False


# ── provisioning ─────────────────────────────────────────────────────

def set_factor(name: str, secret: bytes, unlock_with: Optional[dict] = None) -> None:
    """Create the vault (first factor) or add/replace a keyslot for *name*.

    Adding to an existing vault requires the DEK, obtained by unwrapping with *unlock_with* (a dict
    of currently-available factor secrets) or with *name* itself if it was already a slot. Raises
    :class:`NeedExistingFactor` if the vault exists and cannot be unlocked.
    """
    hdr = _load_hdr()
    if not hdr:
        dek = os.urandom(32)
        salt = os.urandom(16)
        nonce = os.urandom(12)
        hdr = {
            "version": 1,
            "salt": salt.hex(),
            "slots": {name: _wrap(dek, _scrypt(secret, salt))},
            "check": {"nonce": nonce.hex(), "ct": AESGCM(dek).encrypt(nonce, _CHECK_TOKEN, None).hex()},
        }
        _save_hdr(hdr)
        return
    avail = dict(unlock_with or {})
    # A re-set of the SAME factor can unlock itself with the new secret — but only if the caller did
    # not supply that factor's CURRENT secret in unlock_with. When CHANGING a slot's secret (e.g. a
    # password change), unlock_with[name] is the OLD secret that still unwraps the DEK; overwriting it
    # with the new secret here would leave no way to unwrap and desync the slot (SEC).
    avail.setdefault(name, secret)
    dek = _dek_from(hdr, avail)
    if dek is None:
        raise NeedExistingFactor(
            f"cannot add the '{name}' keyslot: provide an existing factor (password or present key) "
            "that already unlocks the vault."
        )
    salt = bytes.fromhex(hdr["salt"])
    hdr.setdefault("slots", {})[name] = _wrap(dek, _scrypt(secret, salt))
    _save_hdr(hdr)


def remove_factor(name: str) -> None:
    hdr = _load_hdr()
    slots = hdr.get("slots", {})
    if name in slots and len(slots) > 1:
        del slots[name]
        _save_hdr(hdr)


# ── open / use ───────────────────────────────────────────────────────

class Vault:
    """Decrypted handle over the vault data (AES-256-GCM under the DEK). Data on disk is ciphertext."""

    def __init__(self, dek: bytes) -> None:
        self._dek = dek

    def load(self) -> dict:
        p = _data_path()
        if not p.exists():
            return {}
        blob = p.read_bytes()
        # A GCM nonce (12) + auth tag (16) is the minimum valid ciphertext; anything shorter is
        # truncated/corrupt. Wrap decrypt + JSON parse so a tampered/truncated vault surfaces as ONE
        # clean, fail-closed error instead of a raw InvalidTag/JSONDecodeError crashing the caller
        # (mirrors SecureStorage.decrypt). Never returns plaintext on failure.
        if len(blob) < 12 + 16:
            raise ValueError("vault data corrupt or tampered")
        nonce, ct = blob[:12], blob[12:]
        try:
            pt = AESGCM(self._dek).decrypt(nonce, ct, None)
            return json.loads(pt.decode("utf-8"))
        except (InvalidTag, ValueError, UnicodeDecodeError) as exc:
            raise ValueError("vault data corrupt or tampered") from exc

    def save(self, data: dict) -> None:
        p = _data_path()
        secure_dir(p.parent)
        nonce = os.urandom(12)
        ct = AESGCM(self._dek).encrypt(nonce, json.dumps(data).encode("utf-8"), None)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(nonce + ct)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        restrict_to_current_user(p)

    def get(self, key: str, default=None):
        return self.load().get(key, default)

    def set(self, key: str, value) -> None:
        data = self.load()
        data[key] = value
        self.save(data)


def open_vault(available: dict) -> Optional[Vault]:
    """Return a :class:`Vault` if any of *available* {factor: secret} unwraps the DEK, else None."""
    hdr = _load_hdr()
    if not hdr:
        return None
    dek = _dek_from(hdr, available)
    return Vault(dek) if dek is not None else None
