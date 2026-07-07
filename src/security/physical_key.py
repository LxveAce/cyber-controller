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

import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import string
import threading
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
        # A config file EXISTS but can't be read/parsed. Do NOT collapse this to the unconfigured
        # default (that would silently turn a gate the owner configured into a no-op that grants
        # access — SEC-C2). Mark it corrupt so the gate fails CLOSED. An ABSENT file (above) stays a
        # legitimate unconfigured no-op so a fresh install isn't locked out.
        log.warning("Access-gate config present but UNREADABLE/corrupt — failing closed")
        return {"version": 1, "policy": DEFAULT_POLICY, "password": None, "key": None,
                "__corrupt__": True}
    if not isinstance(cfg, dict):
        # Valid JSON but not an object ([], null, 123, "str") — same class as unparseable. Fail CLOSED and,
        # crucially, don't fall through to cfg.setdefault() below (which raises AttributeError on a non-dict
        # and would break the --clear-gate corrupt-config recovery path, bricking the owner).
        log.warning("Access-gate config is valid JSON but not an object — failing closed")
        return {"version": 1, "policy": DEFAULT_POLICY, "password": None, "key": None,
                "__corrupt__": True}
    cfg.setdefault("policy", DEFAULT_POLICY)
    cfg.setdefault("password", None)
    cfg.setdefault("key", None)
    return cfg


def save_config(cfg: dict) -> None:
    # Never persist the transient corrupt sentinel into a fresh, valid file.
    cfg = {k: v for k, v in cfg.items() if k != "__corrupt__"}
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


# ── atomic read-modify-write of the gate config ──────────────────────
# The failure counter is the backbone of the brute-force lockout, so incrementing it must be
# atomic. A bare load_config()→mutate→save_config() is a lost-update race: two concurrent unlock
# attempts (threads OR separate "relaunch and keep guessing" processes) both read N and both write
# N+1, so an increment vanishes and the counter never reaches the lockout threshold. _STATE_LOCK
# serializes threads in this process; _config_file_lock() serializes separate processes.
_STATE_LOCK = threading.Lock()


@contextlib.contextmanager
def _config_file_lock():
    """Best-effort cross-process advisory lock on the gate config. If the platform primitive is
    unavailable, degrade to no cross-process lock (the in-process _STATE_LOCK still holds) rather
    than break the security path. Only ever entered by one thread at a time (under _STATE_LOCK),
    so there is no intra-process re-lock contention."""
    lock_path = _config_path().with_suffix(".lock")
    try:
        secure_dir(lock_path.parent)
    except Exception:
        pass
    fh = None
    try:
        try:
            fh = open(lock_path, "a+b")
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except (OSError, ImportError):
            pass  # advisory only — the in-process lock still serializes threads here
        yield
    finally:
        if fh is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except (OSError, ImportError):
                pass
            try:
                fh.close()
            except OSError:
                pass


def _locked_config_update(mutate):
    """Atomically load→mutate→save the gate config under both locks. `mutate(cfg)` edits cfg in
    place; if it returns False the write is skipped (nothing changed). Returns mutate's value."""
    with _STATE_LOCK:
        with _config_file_lock():
            cfg = load_config()
            result = mutate(cfg)
            if result is not False:
                save_config(cfg)
    return result


# ── status ───────────────────────────────────────────────────────────

def _now() -> float:
    import time
    return time.time()


# Brute-force lockout + duress wipe. A persistent failure counter (in the gate config, surviving a
# restart) defeats a "relaunch and keep guessing" brute force; after _LOCKOUT_AFTER consecutive
# failures an exponential, capped cooldown applies. If the owner OPTS IN (wipe_on_failures > 0),
# reaching that threshold triggers an anti-forensic wipe of the app's own secrets. Recorded centrally
# in check_access() so every unlock path (console / Qt / web) is covered.
_LOCKOUT_AFTER = 5
_LOCKOUT_BASE_SECS = 30
_LOCKOUT_MAX_SECS = 3600


def _lockout_remaining(cfg: dict) -> int:
    """Seconds left in the current cooldown (0 = not locked). Exponential after _LOCKOUT_AFTER."""
    fails = int(cfg.get("failed_attempts", 0) or 0)
    if fails < _LOCKOUT_AFTER:
        return 0
    last = float(cfg.get("last_failure_ts", 0) or 0)
    cooldown = min(_LOCKOUT_BASE_SECS * (2 ** (fails - _LOCKOUT_AFTER)), _LOCKOUT_MAX_SECS)
    return max(0, int(last + cooldown - _now()))


def lockout_status() -> dict:
    cfg = load_config()
    rem = _lockout_remaining(cfg)
    return {"failed_attempts": int(cfg.get("failed_attempts", 0) or 0),
            "locked": rem > 0, "remaining_secs": rem}


def record_successful_unlock() -> None:
    """Reset the persistent failure counter after a successful unlock (atomically)."""
    def _reset(cfg):
        if not (cfg.get("failed_attempts") or cfg.get("last_failure_ts") or cfg.get("wipe_failures")):
            return False  # nothing to reset — skip the write
        cfg["failed_attempts"] = 0
        cfg["last_failure_ts"] = 0
        cfg["wipe_failures"] = 0  # a successful unlock disarms the wipe counter too
    _locked_config_update(_reset)


def record_failed_attempt(*, allow_wipe: bool = True) -> dict:
    """Increment the persistent counter, persist it, and fire the opt-in duress wipe if the
    configured threshold is reached. Returns the new lockout status (+ 'wipe_triggered').

    The increment is done under _locked_config_update so concurrent attempts can't lose it.

    *allow_wipe* gates the destructive duress wipe. It defaults True for the LOCAL console/Qt unlock
    surfaces. The network-facing web remote passes allow_wipe=False: the duress wipe is a PHYSICAL
    anti-forensic control (seizure), and a remote/pre-auth network actor must never be able to drive an
    irreversible wipe of the vault + gate config.

    The wipe is armed by a SEPARATE 'wipe_failures' counter that only local (allow_wipe) failures advance —
    the shared 'failed_attempts' lockout counter stays shared across all surfaces (SEC-A1), but a network
    actor must not be able to pre-load the wipe counter so a later local slip trips it early."""
    def _inc(cfg):
        cfg["failed_attempts"] = int(cfg.get("failed_attempts", 0) or 0) + 1
        cfg["last_failure_ts"] = _now()
        if allow_wipe:  # only LOCAL failures may arm the physical duress wipe
            cfg["wipe_failures"] = int(cfg.get("wipe_failures", 0) or 0) + 1
        return int(cfg.get("wipe_on_failures", 0) or 0), int(cfg.get("wipe_failures", 0) or 0)
    wipe_at, wipe_fails = _locked_config_update(_inc)
    wiped = False
    if allow_wipe and wipe_at > 0 and wipe_fails >= wipe_at:  # duress wipe outside the lock (it may be slow)
        wiped = trigger_duress_wipe()
    st = lockout_status()
    st["wipe_triggered"] = wiped
    return st


def set_wipe_on_failures(threshold: int) -> None:
    """OPT-IN duress self-wipe: after *threshold* consecutive failed unlocks, the app's secrets are
    wiped. 0 disables (default). Owner-set knowingly; keep it well above _LOCKOUT_AFTER."""
    def _set(cfg):
        cfg["wipe_on_failures"] = max(0, int(threshold))
    _locked_config_update(_set)


def disarm_duress_wipe() -> None:
    """Fully disarm the opt-in duress wipe and clear the failure/lockout counters. Called on a GATE CLEAR
    (--clear-gate): removing the unlock factors must ALSO remove the destructive threshold. Otherwise the
    threshold persists in the config, and because a cleared gate is no longer 'configured' the subsequent
    reprovision (--set-admin-password) runs UNAUTHENTICATED as first-time setup and silently inherits the
    old wipe_on_failures — so a few failed unlocks on the new gate would irreversibly wipe secrets the owner
    never re-opted into on it."""
    def _disarm(cfg):
        if not any(cfg.get(k) for k in ("wipe_on_failures", "wipe_failures",
                                        "failed_attempts", "last_failure_ts")):
            return False  # nothing armed / counted — skip the write
        cfg["wipe_on_failures"] = 0
        cfg["wipe_failures"] = 0
        cfg["failed_attempts"] = 0
        cfg["last_failure_ts"] = 0
    _locked_config_update(_disarm)


def _secure_delete(path: Path) -> bool:
    """Best-effort secure delete: overwrite content then unlink. Returns True ONLY when the file is
    verifiably gone afterwards — an anti-forensic control must never report a wipe it did not perform,
    so a swallowed PermissionError (file held open / read-only) must surface as a False result to the
    caller. (Honest caveat: on SSDs with wear-levelling/TRIM, overwrite is not a forensic guarantee —
    it destroys the live copy.)"""
    try:
        if path.exists() and path.is_file():
            n = max(path.stat().st_size, 1)
            with open(path, "r+b", buffering=0) as fh:
                for _ in range(2):
                    fh.seek(0)
                    fh.write(os.urandom(n))
                    fh.flush()
                    os.fsync(fh.fileno())
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
    return not path.exists()


def trigger_duress_wipe() -> bool:
    """Anti-forensic wipe of the app's OWN secrets (gate config + encrypted vault) on a duress
    failed-attempt threshold. Best-effort secure overwrite+delete; NEVER touches anything outside the
    app's own data. Returns True ONLY when every targeted secret is verifiably gone afterwards — a wipe
    that could not destroy a file (held open, read-only, ACL) must NOT be reported as success, or the
    owner is falsely told their secrets were destroyed while recoverable ciphertext remains on disk.
    Defeats casual/seizure access to the secrets, NOT a forensic adversary on modern SSDs."""
    log.warning("Duress wipe triggered (failed-attempt threshold reached) — destroying app secrets.")
    attempted = False   # did we target at least one existing secret?
    all_gone = True     # is every targeted secret verifiably gone?
    try:
        from src.security import vault
        for p in (vault._data_path(), vault._hdr_path()):
            if Path(p).exists():
                attempted = True
                if not _secure_delete(Path(p)):
                    all_gone = False
                    log.error("duress wipe: FAILED to destroy %s — secret may remain on disk", p)
    except Exception:
        all_gone = False
        log.exception("duress wipe: vault destruction failed")
    try:
        cp = _config_path()
        if cp.exists():
            attempted = True
            if not _secure_delete(cp):
                all_gone = False
                log.error("duress wipe: FAILED to destroy %s — secret may remain on disk", cp)
    except Exception:
        all_gone = False
        log.exception("duress wipe: gate-config destruction failed")
    try:
        import shutil
        secure_dir = _CONFIG_DIR / "secure"   # the secure_store container
        if secure_dir.exists():
            for p in secure_dir.rglob("*"):
                if p.is_file():
                    attempted = True
                    if not _secure_delete(p):
                        all_gone = False
                        log.error("duress wipe: FAILED to destroy %s — secret may remain on disk", p)
            shutil.rmtree(secure_dir, ignore_errors=True)
            if secure_dir.exists():
                all_gone = False
                log.error("duress wipe: FAILED to remove secure container %s — secrets may remain", secure_dir)
    except Exception:
        all_gone = False
        log.exception("duress wipe: secure-container destruction failed")
    # Truthful status: only claim a wipe when we actually targeted secrets AND all of them are gone.
    return attempted and all_gone


def is_configured() -> bool:
    cfg = load_config()
    return bool(cfg.get("password") or cfg.get("key"))


def config_is_corrupt() -> bool:
    """True if a gate config file exists but couldn't be parsed. Callers must fail CLOSED: a
    configured gate that becomes unreadable must never silently degrade to the no-gate no-op."""
    return bool(load_config().get("__corrupt__"))


def get_policy() -> str:
    return load_config().get("policy", DEFAULT_POLICY)


def set_policy(policy: str) -> None:
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {POLICIES}")
    cfg = load_config()
    # Refuse an exclusive policy whose required factor is not configured — otherwise _evaluate_policy
    # can never grant, and because every gate mutation runs enforce() first, the owner is locked out
    # of the app AND of correcting the policy in-app (recovery = deleting the gate config + vault =
    # data loss). 'both'/'either' stay allowed: _evaluate_policy only requires factors that exist.
    if policy == "key" and not cfg.get("key"):
        raise ValueError("cannot set policy 'key': no physical key is configured — create one first")
    if policy == "password" and not cfg.get("password"):
        raise ValueError("cannot set policy 'password': no admin password is set — set one first")
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

def _evaluate_policy(cfg: dict, password: Optional[str], drives: Optional[list[Path]]) -> tuple[bool, str]:
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


def check_access(password: Optional[str] = None, drives: Optional[list[Path]] = None) -> tuple[bool, str]:
    """Evaluate the configured policy with persistent brute-force lockout + opt-in duress wipe.
    Returns ``(granted, reason)``.

    An unconfigured gate grants access (no-op). Every call is a real unlock attempt (console / Qt /
    web all route here), so the persistent failure counter + cooldown + wipe live here — covering all
    paths and surviving restarts.
    """
    cfg = load_config()
    if cfg.get("__corrupt__"):
        # Present-but-unreadable config: fail closed rather than grant as "unconfigured" (SEC-C2).
        return False, "access denied — gate configuration is unreadable/corrupt (restore it or reset the gate)"
    if not (cfg.get("password") or cfg.get("key")):
        return True, "no gate configured"
    rem = _lockout_remaining(cfg)
    if rem > 0:
        return False, f"locked: too many failed attempts — try again in {rem}s"
    granted, reason = _evaluate_policy(cfg, password, drives)
    if granted:
        record_successful_unlock()
        return True, reason
    st = record_failed_attempt()
    if st.get("wipe_triggered"):
        return False, "access denied — failed-attempt threshold reached; secure wipe triggered"
    extra = f" (failed: {st['failed_attempts']})"
    if st["locked"]:
        extra += f"; locked for {st['remaining_secs']}s"
    return False, reason + extra
