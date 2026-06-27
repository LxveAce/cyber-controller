"""Access-gate runtime + setup UX (console + Qt) on top of :mod:`src.security.physical_key`.

Two responsibilities:
  * :func:`enforce` — called once at startup before any UI launches. Fail-closed: if a gate is
    configured it must be satisfied or the process exits. GUI launches use a Qt dialog (a console
    ``getpass`` is invisible under a ``--windowed`` PyInstaller build); console/headless launches
    use ``getpass``.
  * the ``*_cli`` helpers back the ``--create-physical-key`` / ``--set-admin-password`` /
    ``--gate-policy`` / ``--gate-status`` / ``--clear-gate`` management flags.
"""

from __future__ import annotations

import getpass
import logging
import sys

from src.security import physical_key as pk

log = logging.getLogger(__name__)

_MAX_TRIES = 5


# ── enforcement (startup) ────────────────────────────────────────────

def enforce(ui: str | None) -> bool:
    """Return True if access is granted (or no gate is configured); False if denied/cancelled."""
    if not pk.is_configured():
        return True
    log.info("Access gate active (policy=%s) — authentication required.", pk.get_policy())
    gui = ui in ("qt", "tk", None)
    if gui:
        try:
            from src.ui.qt.access_gate_dialog import unlock_gui
            return unlock_gui()
        except Exception:  # pragma: no cover - depends on Qt availability
            log.warning("Qt gate dialog unavailable — falling back to console prompt.", exc_info=True)
    return _unlock_console()


def _unlock_console() -> bool:
    cfg = pk.load_config()
    need_pw = cfg.get("password") is not None and pk.get_policy() in ("both", "either", "password")
    if pk.get_policy() in ("key",) and not need_pw:
        print("Insert the physical key USB, then press Enter (Ctrl+C to cancel)...", file=sys.stderr)
    for attempt in range(1, _MAX_TRIES + 1):
        pw = None
        try:
            if need_pw:
                pw = getpass.getpass("Admin password: ") or None
            elif pk.get_policy() == "key":
                input()  # wait for the user to insert the key
        except (EOFError, KeyboardInterrupt):
            return False
        granted, reason = pk.check_access(password=pw)
        if granted:
            return True
        print(f"Access denied: {reason}  (attempt {attempt}/{_MAX_TRIES})", file=sys.stderr)
    return False


# ── setup / management (CLI) ─────────────────────────────────────────

def status_cli() -> int:
    cfg = pk.load_config()
    print("=== Cyber Controller — access gate status ===")
    print(f"  configured : {pk.is_configured()}")
    print(f"  policy     : {cfg.get('policy')}")
    print(f"  password   : {'set' if cfg.get('password') else 'not set'}")
    kid = (cfg.get("key") or {}).get("key_id") if cfg.get("key") else None
    print(f"  physical   : {'set (' + kid + ')' if kid else 'not set'}")
    if cfg.get("key"):
        print(f"  key present now: {pk.key_present()}")
    return 0


def set_password_cli() -> int:
    print("=== Set Cyber Controller admin password ===")
    pw = getpass.getpass("  New admin password: ")
    pw2 = getpass.getpass("  Confirm: ")
    if not pw or pw != pw2:
        print("Passwords empty or do not match — aborted.", file=sys.stderr)
        return 2
    pk.set_admin_password(pw)
    print("Admin password set (stored as a salted scrypt verifier; never in plaintext).")
    _print_policy_hint()
    return 0


def set_policy_cli(policy: str) -> int:
    pk.set_policy(policy)
    print(f"Access-gate policy set to: {policy}")
    return 0


def clear_cli() -> int:
    pk.clear_admin_password()
    pk.remove_physical_key()
    print("Access gate cleared — the app will start without authentication.")
    print("(The key file on the USB is left in place; delete it manually if desired.)")
    return 0


def create_key_cli(key_drive: str | None = None) -> int:
    print("=== Create Cyber Controller physical key ===")
    print("Provisions a USB stick as an unlock key. Owner-only defensive use on hardware you own.\n")
    target = key_drive
    if not target:
        drives = pk.list_removable_drives()
        if not drives:
            print("No removable drives detected. Insert a USB stick and retry, or pass "
                  "--key-drive <path>.", file=sys.stderr)
            return 1
        print("  Detected removable drives:")
        for i, d in enumerate(drives, 1):
            print(f"    {i}) {d}")
        raw = input("  Pick a drive number (or type a path): ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(drives):
            target = str(drives[int(raw) - 1])
        elif raw:
            target = raw
        else:
            print("No drive chosen — aborted.", file=sys.stderr)
            return 2
    try:
        kid = pk.create_physical_key(target)
    except (OSError, NotADirectoryError) as exc:
        print(f"Failed to write the key to {target}: {exc}", file=sys.stderr)
        return 1
    print(f"\nPhysical key {kid} written to {target} (as {pk.KEY_FILENAME}).")
    print("Keep this USB safe. Anyone with this file (or a copy) holds the key — this deters casual")
    print("access, it is not proof against an adversary who can copy the USB.")
    _print_policy_hint()
    return 0


def _print_policy_hint() -> None:
    print(f"\nCurrent policy: {pk.get_policy()}  (default 'both' = admin password AND physical key).")
    print("Change with:  cyber-controller --gate-policy {both|either|password|key}")
