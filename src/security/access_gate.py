"""Access-gate runtime + setup UX (console + Qt) on top of :mod:`src.security.physical_key` and the
gate-keyed encrypted :mod:`src.security.vault`.

Responsibilities:
  * :func:`enforce` — called once at startup before any UI/device bootstrap. FAIL-CLOSED:
      - if a gate is configured it must be satisfied or the process exits;
      - if an encrypted vault exists but NO gate is configured, refuse to start (tamper / the gate
        config was removed) — so the launch sequence cannot be bypassed to reach protected data;
      - on success, the vault is opened with the factor(s) actually supplied and exposed via
        :func:`get_current_vault` for the session (data stays encrypted at rest until then).
  * the ``*_cli`` helpers back the management flags and provision the vault keyslots.
"""

from __future__ import annotations

import getpass
import logging
import sys
from typing import Optional, Tuple

from src.security import physical_key as pk
from src.security import vault

log = logging.getLogger(__name__)

_MAX_TRIES = 5
_CURRENT_VAULT: Optional["vault.Vault"] = None


def get_current_vault() -> Optional["vault.Vault"]:
    """The vault opened at unlock for this session, or None (locked / not provisioned)."""
    return _CURRENT_VAULT


# ── enforcement (startup) ────────────────────────────────────────────

def enforce(ui: str | None) -> bool:
    """Return True if access is granted (or no gate is configured); False if denied/cancelled."""
    global _CURRENT_VAULT
    if pk.config_is_corrupt():
        # A gate config file exists but can't be parsed. Refuse to start regardless of whether a
        # vault is present — a corrupt config must never silently degrade to the no-gate no-op that
        # would open the app without authentication (SEC-C2).
        log.error("Access-gate config present but unreadable/corrupt — refusing to start "
                  "(fail-closed).")
        print("Locked: the access-gate configuration is unreadable/corrupt. Restore it or reset the "
              "gate to proceed.", file=sys.stderr)
        return False
    if not pk.is_configured():
        if vault.exists():
            # An encrypted vault is present but the gate is gone -> fail closed. This stops a
            # "delete the gate config to skip the opening sequence" bypass: the app will not start,
            # and the vault data stays encrypted regardless.
            log.error("Encrypted vault present but no access gate is configured — refusing to start "
                      "(fail-closed; gate config missing or tampered).")
            print("Locked: the access-gate configuration is missing but an encrypted vault exists. "
                  "Restore the gate config or remove the vault to proceed.", file=sys.stderr)
            return False
        return True

    log.info("Access gate active (policy=%s) — authentication required.", pk.get_policy())
    gui = ui in ("qt", "tk", None)
    ok, pw = (False, None)
    if gui:
        try:
            from src.ui.qt.access_gate_dialog import unlock_gui
            ok, pw = unlock_gui()
        except Exception:  # pragma: no cover - Qt availability
            log.warning("Qt gate dialog unavailable — falling back to console prompt.", exc_info=True)
            ok, pw = _unlock_console()
    else:
        ok, pw = _unlock_console()
    if not ok:
        return False

    # Open the gate-keyed vault with the factor(s) actually provided. Until this point the data is
    # ciphertext on disk and the key material does not exist in memory.
    if vault.is_provisioned():
        avail = {}
        if pw:
            avail["password"] = pw.encode("utf-8")
        try:
            secret = pk.present_key_secret() if pk.has_physical_key() else None
        except Exception:  # pragma: no cover
            secret = None
        if secret is not None:
            avail["key"] = secret
        try:
            _CURRENT_VAULT = vault.open_vault(avail)
        except Exception:  # pragma: no cover
            _CURRENT_VAULT = None
        if _CURRENT_VAULT is None:
            log.warning("Gate satisfied but the encrypted vault could not be opened with the "
                        "supplied factor(s); protected data stays locked this session.")
    return True


def _unlock_console() -> Tuple[bool, Optional[str]]:
    cfg = pk.load_config()
    need_pw = cfg.get("password") is not None and pk.get_policy() in ("both", "either", "password")
    # Pause for the operator to insert the USB key whenever a key is configured and we are NOT
    # collecting a password this round. Guarding this on policy=='key' alone meant a key-only gate
    # under the DEFAULT 'both' (or 'either') policy — the state left by `--create-physical-key` with
    # no admin password — never blocked: it burned all _MAX_TRIES against an absent key in
    # milliseconds, tripping the persistent lockout and any opt-in duress self-wipe on a normal boot.
    wait_for_key = (not need_pw) and cfg.get("key") is not None
    if wait_for_key:
        print("Insert the physical key USB, then press Enter (Ctrl+C to cancel)...", file=sys.stderr)
    for attempt in range(1, _MAX_TRIES + 1):
        pw = None
        try:
            if need_pw:
                pw = getpass.getpass("Admin password: ") or None
            elif wait_for_key:
                input()
        except (EOFError, KeyboardInterrupt):
            return False, None
        granted, reason = pk.check_access(password=pw)
        if granted:
            return True, pw
        print(f"Access denied: {reason}  (attempt {attempt}/{_MAX_TRIES})", file=sys.stderr)
    return False, None


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
    print(f"  encrypted vault: {'provisioned (' + ', '.join(vault.factors()) + ')' if vault.is_provisioned() else 'none'}")
    return 0


def set_password_cli() -> int:
    print("=== Set Cyber Controller admin password ===")
    # Gather the factor(s) that already unlock an EXISTING vault so we can RE-KEY it under the new
    # password. Re-keying the 'password' slot needs a currently-valid factor to unwrap the DEK: the
    # physical key (if present) OR the OLD password. Without this, CHANGING a password-only vault's
    # password desyncs the gate verifier from the vault keyslot and permanently locks the vault (the
    # new password passes the gate but can no longer unwrap the DEK).
    unlock = {}
    # Only try to unwrap with the physical key if it is actually a vault keyslot. The gate verifier and
    # the vault can drift out of sync (e.g. --create-physical-key stored the gate key but the vault
    # set_factor('key',…) was rejected for a missing admin password), leaving has_physical_key() True
    # while the vault has no 'key' slot. Unlocking with a non-existent slot can't unwrap the DEK and,
    # by populating `unlock`, would skip the current-password fallback below — permanently blocking the
    # change while the USB is inserted. Mirror the 'password' branch's `in vault.factors()` guard.
    if pk.has_physical_key() and vault.is_provisioned() and "key" in vault.factors():
        ks = pk.present_key_secret()
        if ks:
            unlock["key"] = ks
    if not unlock and vault.is_provisioned() and "password" in vault.factors():
        existing = getpass.getpass("  Current admin password (to re-key the vault): ") or None
        if existing:
            unlock["password"] = existing.encode("utf-8")
    pw = getpass.getpass("  New admin password: ")
    pw2 = getpass.getpass("  Confirm: ")
    if not pw or pw != pw2:
        print("Passwords empty or do not match — aborted.", file=sys.stderr)
        return 2
    # Re-key the vault BEFORE committing the new gate verifier so the two stay in sync: if the vault
    # can't be unlocked to re-key it, abort WITHOUT changing the gate password (both keep their old,
    # matching state) rather than leaving the gate on the new password and the vault on the old one.
    try:
        vault.set_factor("password", pw.encode("utf-8"), unlock_with=unlock or None)
    except vault.NeedExistingFactor:
        options = []
        if vault.is_provisioned() and "password" in vault.factors():
            options.append("the current admin password")
        if pk.has_physical_key():
            options.append("insert the physical key")
        need = " or ".join(options) if options else "an existing unlock factor"
        print(f"Admin password NOT changed: the encrypted vault could not be unlocked to re-key it. "
              f"Provide {need} and re-run --set-admin-password.", file=sys.stderr)
        return 2
    pk.set_admin_password(pw)
    print("Admin password set; encrypted vault keyslot provisioned (salted scrypt; no plaintext).")
    _print_policy_hint()
    return 0


def set_policy_cli(policy: str) -> int:
    try:
        pk.set_policy(policy)
    except ValueError as exc:
        print(f"Cannot set policy: {exc}", file=sys.stderr)
        return 2
    print(f"Access-gate policy set to: {policy}")
    return 0


def clear_cli() -> int:
    pk.clear_admin_password()
    pk.remove_physical_key()
    pk.disarm_duress_wipe()  # a gate clear must also remove the opt-in destructive threshold (not just factors)
    if vault.exists():
        # The gate factors are gone, but an encrypted vault still on disk makes the NEXT launch fail
        # closed: enforce() refuses to start when a vault exists with no configured gate (the anti-tamper
        # branch). So we must NOT promise a prompt-free start here — that would be false for every normally
        # provisioned gate (set-admin-password / create-physical-key / the Qt dialog all write the vault
        # first). Tell the owner the truth and exactly what remains to finish the clear.
        print("Access-gate factors cleared (admin password, physical key, and duress threshold removed).")
        print("IMPORTANT: an encrypted vault is still present, so the app will stay LOCKED on the next "
              "launch (fail-closed) until the vault is also removed. To finish clearing the gate, delete:")
        print(f"    {vault._data_path()}")
        print(f"    {vault._hdr_path()}")
        print("The vault stays encrypted until then; once removed, the app starts without prompting.")
        return 0
    print("Access gate cleared — the app will start without prompting.")
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
    # Provision the vault keyslot for the key (creating the vault on first factor).
    from pathlib import Path
    secret = pk._read_key_secret(Path(target) / pk.KEY_FILENAME)
    unlock = {}
    if pk.has_admin_password():
        existing = getpass.getpass("  Existing admin password (to add this key to the vault): ") or None
        if existing:
            unlock["password"] = existing.encode("utf-8")
    try:
        if secret is not None:
            vault.set_factor("key", secret, unlock_with=unlock or None)
            print("Encrypted vault keyslot provisioned for the physical key.")
    except vault.NeedExistingFactor:
        print("The vault keyslot for this key was NOT added (existing admin password required). "
              "Re-run --create-physical-key and enter the admin password to add it.", file=sys.stderr)
    print("Keep this USB safe. Anyone with this file (or a copy) holds the key — this deters casual")
    print("access; it is not proof against an adversary who can copy the USB.")
    _print_policy_hint()
    return 0


def _print_policy_hint() -> None:
    print(f"\nCurrent policy: {pk.get_policy()}  (default 'both' = admin password AND physical key).")
    print("Change with:  cyber-controller --gate-policy {both|either|password|key}")
