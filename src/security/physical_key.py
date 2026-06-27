"""Physical-key access gate — unlock the app with an admin password and/or a provisioned USB key.

Owner-only, DEFENSIVE access control for a controller you own. The gate is FAIL-CLOSED: if a
policy is configured, the app does not start until it is satisfied (or the user cancels, which
exits). An unconfigured gate is a no-op (the app starts normally) — this is the safe default.

Design (honest threat model: this DETERS casual/opportunistic access; it is NOT proof against a
funded forensic adversary who can image the disk and the USB):

  * "Create physical key" generates a random 32-byte secret, writes it to a chosen USB volume as
    ``.cyber-controller-key.json``, and stores ONLY a scrypt VERIFIER (salted hash) of that secret
    in the app config. The secret itself never lives in the app config, so a copied app config
    cannot reconstruct the key, and the stored verifier cannot be replayed onto a USB without the
    original secret. (A determined attacker who copies the USB file can clone the key — documented.)
  * The admin password is likewise stored only as a salted scrypt verifier.
  * Policy: ``both`` (password AND key, default — highest assurance), ``either`` (password OR key),
    ``password`` (password only), ``key`` (key only).

All crypto is stdlib (``hashlib.scrypt`` + ``hmac.compare_digest``). The config is written
owner-only (NTFS ACL on Windows via win_acl; 0600 elsewhere). Secrets are never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import string
from pathlib import Path
from typing import Optional

from src.security.win_acl import restrict_to_current_user, secure_dir

log = logging.getLogger(__name__)

KEY_FILENAME = ".cyber-controller-key.json"
_CONFIG_NAME = "access_gate.json"
_CONFIG_DIR = Path.home() / ".cyber-controller"

# scrypt work factors (interactive-unlock grade; matches encrypted_storage).
_N, _R, _P, _DKLEN = 2 ** 15, 8, 1, 32
_MAXMEM = 128 * 1024 * 1024  # headroom so N=2**15 doesn't trip OpenSSL's default maxmem cap

POLICIES = ("both", "either", "password", "key")
DEFAULT_POLICY = "both"


# ── config location (overridable for tests) ──────────────────────────

def _config_path() -> Path:
    override = os.environ.get("CC_GATE_CONFIG")
    return Path(override) if override else (_CONFIG_DIR / _CONFIG_NAME)


# ── scrypt verifier helpers ──────────────────────────────────────────

def _scrypt(data: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(data, salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN, maxmem=_MAXMEM)


def _make_verifier(data: bytes) -> dict:
    salt = os.urandom(16)
    return {"salt": salt.hex(), "hash": _scrypt(data, salt).hex(), "n": _N, "r": _R, "p": _P}


def _check_verifier(data: bytes, ver: dict) -> bool:
    try:
        salt = bytes.fromhex(ver["salt"])
        want = bytes.fromhex(ver["hash"])
        got = hashlib.scrypt(data, salt=salt, n=int(ver.get("n", _N)), r=int(ver.get("r", _R)),
                             p=int(ver.get("p", _P)), dklen=len(want), maxmem=_MAXMEM)
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


# ── config persistence ───────────────────────────────────────────────

def load_config() -> dict:
    path = _config_path()
    if not path.exists():
        return {"version": 1, "policy": DEFAULT_POLICY, "password": None, "key": None}
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("Access-gate config unreadable — treating as unconfigured")
        return {"version": 1, "policy": DEFAULT_POLICY, "password": None, "key": None}
    cfg.setdefault("policy", DEFAULT_POLICY)
    cfg.setdefault("password", None)
    cfg.setdefault("key", None)
    return cfg


def save_config(cfg: dict) -> None:
    path = _config_path()
    secure_dir(path.parent)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    finally:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    restrict_to_current_user(path)


# ── status ───────────────────────────────────────────────────────────

def is_configured() -> bool:
    cfg = load_config()
    return bool(cfg.get("password") or cfg.get("key"))


def get_policy() -> str:
    return load_config().get("policy", DEFAULT_POLICY)


def set_policy(policy: str) -> None:
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {POLICIES}")
    cfg = load_config()
    cfg["policy"] = policy
    save_config(cfg)


# ── admin password factor ────────────────────────────────────────────

def set_admin_password(password: str) -> None:
    if not password:
        raise ValueError("password must not be empty")
    cfg = load_config()
    cfg["password"] = _make_verifier(password.encode("utf-8"))
    save_config(cfg)


def clear_admin_password() -> None:
    cfg = load_config()
    cfg["password"] = None
    save_config(cfg)


def has_admin_password() -> bool:
    return load_config().get("password") is not None


def verify_admin_password(password: str) -> bool:
    ver = load_config().get("password")
    if not ver or not password:
        return False
    return _check_verifier(password.encode("utf-8"), ver)


# ── physical USB key factor ──────────────────────────────────────────

def _new_key_id() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "CCK-" + "".join(secrets.choice(alphabet) for _ in range(8))


def create_physical_key(usb_dir: str | Path, key_id: Optional[str] = None) -> str:
    """Generate a new key secret, write it to *usb_dir*, and store its verifier in the gate config.

    Returns the key_id. The plaintext secret is written ONLY to the USB key file; the app stores
    only a scrypt verifier of it.
    """
    usb = Path(usb_dir)
    if not usb.is_dir():
        raise NotADirectoryError(f"USB target is not a directory: {usb}")
    kid = key_id or _new_key_id()
    secret = secrets.token_bytes(32)
    key_file = usb / KEY_FILENAME
    payload = {"version": 1, "key_id": kid, "secret": secret.hex(),
               "note": "Cyber Controller physical key — keep this USB safe; do not share."}
    fd = os.open(str(key_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    finally:
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
    cfg = load_config()
    ver = _make_verifier(secret)
    ver["key_id"] = kid
    cfg["key"] = ver
    save_config(cfg)
    log.info("Physical key %s created on %s", kid, usb)
    return kid


def remove_physical_key() -> None:
    """Forget the configured key (does not wipe the USB file)."""
    cfg = load_config()
    cfg["key"] = None
    save_config(cfg)


def has_physical_key() -> bool:
    return load_config().get("key") is not None


def _read_key_secret(key_file: Path) -> Optional[bytes]:
    try:
        data = json.loads(key_file.read_text(encoding="utf-8"))
        return bytes.fromhex(data["secret"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return None


def list_removable_drives() -> list[Path]:
    """Best-effort cross-platform list of mounted REMOVABLE volumes."""
    out: list[Path] = []
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            bitmask = k32.GetLogicalDrives()
            for i, letter in enumerate(string.ascii_uppercase):
                if bitmask & (1 << i):
                    root = f"{letter}:\\"
                    if k32.GetDriveTypeW(ctypes.c_wchar_p(root)) == 2:  # DRIVE_REMOVABLE
                        out.append(Path(root))
        except Exception as exc:  # pragma: no cover - platform/driver dependent
            log.debug("Windows removable-drive scan failed: %s", exc)
    else:
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        roots = [Path("/run/media") / user, Path(f"/media/{user}"), Path("/media"), Path("/Volumes")]
        for base in roots:
            try:
                if base.is_dir():
                    out.extend(p for p in base.iterdir() if p.is_dir())
            except OSError:
                continue
    # de-dup preserving order
    seen, uniq = set(), []
    for p in out:
        if str(p) not in seen:
            seen.add(str(p)); uniq.append(p)
    return uniq


def key_present(drives: Optional[list[Path]] = None) -> bool:
    """True if any *drives* (default: removable drives) hold the provisioned key secret."""
    ver = load_config().get("key")
    if not ver:
        return False
    for drive in (drives if drives is not None else list_removable_drives()):
        secret = _read_key_secret(Path(drive) / KEY_FILENAME)
        if secret is not None and _check_verifier(secret, ver):
            return True
    return False


def present_key_secret(drives: Optional[list[Path]] = None) -> Optional[bytes]:
    """Return the secret of a PRESENT, matching physical key (for vault unlock), or None."""
    ver = load_config().get("key")
    if not ver:
        return None
    for drive in (drives if drives is not None else list_removable_drives()):
        secret = _read_key_secret(Path(drive) / KEY_FILENAME)
        if secret is not None and _check_verifier(secret, ver):
            return secret
    return None


# ── policy evaluation ────────────────────────────────────────────────

def check_access(password: Optional[str] = None, drives: Optional[list[Path]] = None) -> tuple[bool, str]:
    """Evaluate the configured policy. Returns ``(granted, reason)``.

    An unconfigured gate grants access (no-op). ``password`` is the candidate admin password
    (or None if not supplied this round); ``drives`` lets callers/tests inject the drive list.
    """
    cfg = load_config()
    if not (cfg.get("password") or cfg.get("key")):
        return True, "no gate configured"
    policy = cfg.get("policy", DEFAULT_POLICY)
    pw_ok = bool(cfg.get("password")) and password is not None and verify_admin_password(password)
    key_ok = bool(cfg.get("key")) and key_present(drives)

    if policy == "password":
        return (pw_ok, "password ok" if pw_ok else "password required")
    if policy == "key":
        return (key_ok, "key present" if key_ok else "physical key required")
    if policy == "either":
        ok = pw_ok or key_ok
        return (ok, "ok" if ok else "admin password OR physical key required")
    # both (AND) — only require factors that are actually configured
    need_pw = bool(cfg.get("password"))
    need_key = bool(cfg.get("key"))
    ok = ((pw_ok or not need_pw) and (key_ok or not need_key))
    if ok:
        return True, "ok"
    missing = []
    if need_pw and not pw_ok:
        missing.append("admin password")
    if need_key and not key_ok:
        missing.append("physical key")
    return False, " + ".join(missing) + " required"
