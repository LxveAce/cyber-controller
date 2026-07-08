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

import contextlib
import hashlib
import json
import os
import tempfile
import threading
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


# Vault.set() is a load→mutate→save read-modify-write, and the Vault handle is the process-global
# singleton written by MULTIPLE subsystems under their OWN, non-shared locks (node_provision._RES_LOCK,
# secure_store._KEY_LOCK). Without a lock shared across all writers of the SAME vault, a container-key
# mint and a node-table write interleave and the second save clobbers the first key (lost update →
# orphaned ciphertext / dropped node). This registry hands every writer of a given vault dir the SAME
# lock, keyed on the resolved dir, so the read-modify-write is atomic regardless of which subsystem (or
# which Vault instance) initiates it. Reentrant so a future update(key, fn) can nest load()/save().
_DIR_LOCKS: dict[str, threading.RLock] = {}
_DIR_LOCKS_GUARD = threading.Lock()


def _dir_lock() -> threading.RLock:
    key = str(_dir().resolve())
    with _DIR_LOCKS_GUARD:
        lock = _DIR_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _DIR_LOCKS[key] = lock
        return lock


def _hdr_path() -> Path:
    return _dir() / _HDR_NAME


def _data_path() -> Path:
    return _dir() / _DATA_NAME


def _scrypt(secret: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(secret, salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN, maxmem=_MAXMEM)


def _atomic_write(path: Path, data: bytes) -> None:
    """Write *data* to *path* durably and atomically: a unique temp file in the SAME directory,
    fsync'd, then ``os.replace``d into place.

    The header (wrapped DEK + salt) and the data blob are each a single all-or-nothing object, so a
    plain ``O_TRUNC`` rewrite that is interrupted by power loss / an OS kill between the truncate and
    the completed write leaves a 0-length or partial file — and because there is no second copy, that
    permanently destroys every node key and the secure-container key. os.replace is atomic within a
    filesystem, so a crash leaves EITHER the old file or the complete new one, never a torn hybrid;
    the fsync flushes the data blocks before the rename commits. Mirrors flock.checkpoint /
    settings.save_settings, which already write this way. The temp file is created 0600.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


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
    # is_provisioned() is just "file exists", so a truncated/empty/garbage header (e.g. a crash during
    # _save_hdr's O_TRUNC-then-write window, or a 0-byte file) must NOT raise a raw JSONDecodeError out
    # of factors()/set_factor into user-facing management commands (--gate-status runs without auth).
    # Fail closed to an empty header: the encrypted data stays sealed and the caller sees a clean state.
    try:
        hdr = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return hdr if isinstance(hdr, dict) else {}


def _save_hdr(hdr: dict) -> None:
    p = _hdr_path()
    secure_dir(p.parent)
    # vault.hdr.json is the ONLY wrapped copy of the DEK + salt; a torn write makes the whole vault
    # permanently unopenable by every factor, so it must be written atomically (temp + fsync + replace).
    _atomic_write(p, json.dumps(hdr, indent=2).encode("utf-8"))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    restrict_to_current_user(p)


def _dek_from(hdr: dict, available: dict) -> Optional[bytes]:
    # A valid-JSON-but-incomplete header (missing 'salt') must not raise a raw KeyError out of
    # set_factor; treat it as unusable so the caller gets NeedExistingFactor / an empty factor list.
    salt_hex = hdr.get("salt")
    if not salt_hex:
        return None
    salt = bytes.fromhex(salt_hex)
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
        # vault.enc holds every node key + the secure-container key as one all-or-nothing GCM blob.
        # A plain O_TRUNC rewrite interrupted mid-write (power loss / OS kill) would leave it 0-length
        # or partial and permanently unrecoverable, so write it atomically (temp + fsync + replace).
        _atomic_write(p, nonce + ct)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        restrict_to_current_user(p)

    def get(self, key: str, default=None):
        return self.load().get(key, default)

    def set(self, key: str, value) -> None:
        # Serialize the whole load→mutate→save against every other writer of this same vault dir so
        # concurrent sets of DIFFERENT keys (e.g. secure_container_key vs node_keys) can't lose one
        # another's update — see _dir_lock() for why a shared, per-dir lock is required here.
        with _dir_lock():
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
