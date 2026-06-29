"""Secure container for app-saved data (logs / sessions / captures).

When the ``security.secure_container`` setting is ON, app-internal saves are encrypted AT REST in a
container under ``~/.cyber-controller/secure/`` and are unreadable while the access gate is locked.

Key source: a random per-install **container key** stored inside the gate-keyed AES-GCM vault
(:mod:`src.security.vault`, opened at unlock via :func:`access_gate.get_current_vault`). So the
container is sealed whenever the vault is locked / the gate isn't set up — there is no key in the
clear, which is what makes the data "can't be accessed" without the gate.

Hardening: encryption happens in-process and :class:`SecureStorage.save` writes the ciphertext blob
directly (AES-256-GCM, authenticated → tamper fails closed; 0600 + owner-only ACL). NO plaintext copy
is ever written to disk, which closes the interception/recovery window for live log streams.

NOTE: explicit *exports* meant to be shared (e.g. a WiGLE wardrive CSV) deliberately do NOT go through
the container — only app-internal saves do.
"""

from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path
from typing import Any, Optional

from src.security import access_gate
from src.security.encrypted_storage import SecureStorage

_KEY_ENTRY = "secure_container_key"
_DIR = Path.home() / ".cyber-controller" / "secure"


def enabled() -> bool:
    """True if the secure-container setting is ON."""
    try:
        from src.config.settings import load_settings
        return bool(load_settings().get("security", {}).get("secure_container", False))
    except Exception:
        return False


def container_dir() -> Path:
    return _DIR


def entry_path(category: str, name: str) -> Path:
    """Public path of a container entry (the ``.enc`` file). Does not require an unlocked vault."""
    return _path(category, name)


def _container_key() -> Optional[str]:
    """The per-install container key from the unlocked vault (created on first use), or None when the
    gate is locked / not provisioned (→ container sealed)."""
    v = access_gate.get_current_vault()
    if v is None:
        return None
    key = v.get(_KEY_ENTRY)
    if not key:
        key = secrets.token_hex(32)
        v.set(_KEY_ENTRY, key)
    return key


def available() -> bool:
    """True if container mode is ON *and* the vault is unlocked (a key exists)."""
    return enabled() and _container_key() is not None


def _safe_cat(category: str) -> str:
    return "".join(c for c in (category or "misc") if c.isalnum() or c in "-_") or "misc"


def _path(category: str, name: str) -> Path:
    return _DIR / _safe_cat(category) / (os.path.basename(name) + ".enc")


def is_container_path(path: Any) -> bool:
    """True if *path* points inside the secure container (a ``.enc`` file under the container dir)."""
    try:
        p = Path(path).resolve()
    except Exception:
        return False
    if p.suffix != ".enc":
        return False
    try:
        p.relative_to(_DIR.resolve())
        return True
    except ValueError:
        return False


def save(category: str, name: str, data: dict[str, Any]) -> Path:
    """Encrypt + save a dict into the container (ciphertext only). Raises if the container is
    locked/unavailable (caller decides whether to fall back to a plaintext path when the feature is OFF)."""
    key = _container_key()
    if key is None:
        raise RuntimeError("secure container is locked — unlock the access gate first")
    p = _path(category, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    SecureStorage(key).save(data, p)   # AES-256-GCM ciphertext written directly; 0600 + ACL
    return p


def save_text(category: str, name: str, text: str) -> Path:
    return save(category, name, {"name": name, "text": text})


def load(category: str, name: str) -> Optional[dict[str, Any]]:
    """Decrypt a container entry, or None if locked/missing. Raises on tamper (auth failure)."""
    key = _container_key()
    if key is None:
        return None
    p = _path(category, name)
    if not p.exists():
        return None
    return SecureStorage(key).load(p)


def load_text(category: str, name: str) -> Optional[str]:
    d = load(category, name)
    return None if d is None else d.get("text")


def load_file(path: Any) -> Optional[dict[str, Any]]:
    """Decrypt a specific container ``.enc`` file by path, or None if locked/missing. Raises on tamper."""
    key = _container_key()
    if key is None:
        return None
    p = Path(path)
    if not p.exists():
        return None
    return SecureStorage(key).load(p)


def list_names(category: str) -> list[str]:
    """Base names of the entries stored in *category* (empty if the container is sealed/absent)."""
    if _container_key() is None:
        return []
    d = _DIR / _safe_cat(category)
    if not d.is_dir():
        return []
    return sorted(p.name[: -len(".enc")] for p in d.glob("*.enc"))


def wipe() -> None:
    """Securely remove the entire container (used by the access-gate duress wipe)."""
    if not _DIR.exists():
        return
    try:
        from src.security.physical_key import _secure_delete
        for p in _DIR.rglob("*"):
            if p.is_file():
                _secure_delete(p)
    except Exception:
        pass
    shutil.rmtree(_DIR, ignore_errors=True)
