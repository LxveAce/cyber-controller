"""Persistent application settings — JSON-backed with per-section deep-merge.

Settings live at ``~/.cyber-controller/settings.json``.  Loading merges the
saved file on top of :data:`DEFAULTS` section-by-section, so a settings file
written by an older version (missing keys/sections) still yields a complete,
usable config.  The file is written with ``0600`` permissions because it may
hold local paths and operational preferences that should not be world-readable.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

from src.security.win_acl import restrict_to_current_user, secure_dir

log = logging.getLogger(__name__)


# ── Defaults ─────────────────────────────────────────────────────────

# Note: most sections are dicts, but a couple of top-level scalars (e.g.
# "_disclaimer_ack") also live here — hence the ``Any`` value type.
DEFAULTS: dict[str, Any] = {
    "serial": {
        "default_baud": 115200,
        "timeout": 5,
    },
    "flash": {
        "flash_baud": 921600,
        "verify": True,
        "auto_backup": True,
        "mode": "dio",
    },
    "cross_comm": {
        "auto_share": True,
        "dedup_by_mac": True,
    },
    "vault": {
        "dir": str(Path.home() / ".cyber-controller" / "firmware"),
    },
    # Safety / disclaimer system (see src/core/safety.py). These LABEL and warn;
    # they never remove or block a capability — "Yes, proceed" is always available,
    # and suppress_all_warnings turns the friction off entirely.
    "safety": {
        "confirm_dangerous": True,       # show a confirm before a dangerous send
        "suppress_all_warnings": False,  # master off-switch for per-command warnings
    },
    # Secure container: when ON, app-internal saves (logs/sessions/captures) are encrypted at rest
    # in a gate-keyed container (src/security/secure_store.py) and are unreadable while the access
    # gate is locked. Off by default; also offerable as an install option. (Explicit exports like a
    # WiGLE wardrive CSV stay plaintext by design — they're meant to be shared.)
    "security": {
        "secure_container": False,
    },
    # Dual-depth UI (progressive disclosure). "pro" = the full interface (today's behavior, the safe
    # default so an upgrade never hides controls); "simple" = a streamlined view that hides advanced
    # widget groups per tab (each tab's set_ui_mode()). Toggle: View ▸ Interface Mode, Ctrl+M, or the
    # status-bar badge. A first-run prompt lets new users pick Simple. Pro has ZERO feature penalty.
    "interface": {
        "mode": "pro",
    },
    # In-app update check (see src/core/updater.py). A non-blocking startup check asks GitHub for the
    # latest published release and — only when the running build is behind — offers a deep-link to the
    # release page (phase 1 is deep-link only; there is NO self-update / auto-download). The SILENT
    # check always runs when ``enabled``; only the PROMPT is gated by the suppression fields below.
    #   enabled                 master on/off for the automatic startup check (manual check ignores it).
    #   suppressed              user ticked "Don't show again" on a version prompt.
    #   suppressed_at_behind    how many releases behind we were when suppressed (so a NEWER release,
    #                           i.e. behind grows past this, re-arms the prompt — see should_prompt()).
    #   dismissed_version       the latest tag that was dismissed (informational).
    #   offline_error_suppressed  user ticked "Don't show again" on the offline-error dialog. This is
    #                           SEPARATE from the version-suppression fields — the version logic never
    #                           touches it and it never gates the version prompt.
    #   last_seen_latest        last latest tag observed by a check (informational).
    #   last_check_iso          ISO timestamp of the last check that completed (informational).
    "updates": {
        "enabled": True,
        "suppressed": False,
        "suppressed_at_behind": 0,
        "dismissed_version": "",
        "offline_error_suppressed": False,
        "last_seen_latest": "",
        "last_check_iso": "",
    },
    # One-time interface-mode choice prompt shown (so we ask Simple-vs-Pro exactly once).
    "_interface_mode_ack": False,
    # One-time legal/authorized-use disclaimer acknowledgement (top-level scalar,
    # round-trips through _deep_merge). Shown once regardless of suppress_all_warnings.
    "_disclaimer_ack": False,
}

# Directory + file location.  Resolved at import time from the user's home.
SETTINGS_DIR = Path.home() / ".cyber-controller"
SETTINGS_PATH = SETTINGS_DIR / "settings.json"


# ── Internal helpers ─────────────────────────────────────────────────

def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict: *override* layered on top of *base* one level deep.

    Section dicts (``serial``, ``flash``, …) are merged key-by-key so missing
    keys fall back to the *base* (defaults).  Unknown top-level keys in
    *override* are preserved verbatim so forward-compat data is not discarded.
    """
    merged: dict[str, Any] = {}
    for key, base_val in base.items():
        over_val = override.get(key)
        if isinstance(base_val, dict) and isinstance(over_val, dict):
            merged[key] = {**base_val, **over_val}
        elif key in override:
            merged[key] = over_val
        else:
            merged[key] = dict(base_val) if isinstance(base_val, dict) else base_val
    # Carry over any extra sections present in the saved file but not in DEFAULTS.
    for key, over_val in override.items():
        if key not in merged:
            merged[key] = over_val
    return merged


def _defaults_copy() -> dict[str, Any]:
    """Return a deep-ish copy of DEFAULTS (sections copied so callers can mutate)."""
    return {k: dict(v) if isinstance(v, dict) else v for k, v in DEFAULTS.items()}


# ── Public API ───────────────────────────────────────────────────────

def load_settings() -> dict[str, Any]:
    """Load settings from disk, deep-merged onto :data:`DEFAULTS`.

    Returns a complete settings dict even if the file is absent or partial.
    A corrupt/unreadable file logs a warning and falls back to defaults.
    """
    if not SETTINGS_PATH.exists():
        return _defaults_copy()
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("Could not read settings (%s); using defaults", exc)
        return _defaults_copy()
    if not isinstance(saved, dict):
        log.warning("Settings file is not a JSON object; using defaults")
        return _defaults_copy()
    return _deep_merge(DEFAULTS, saved)


def save_settings(settings: dict[str, Any]) -> None:
    """Persist *settings* to disk as JSON with ``0600`` permissions.

    The settings are deep-merged onto :data:`DEFAULTS` before writing so the
    on-disk file is always complete.  The containing directory is created if
    needed.  Written atomically via a temp file + replace.
    """
    merged = _deep_merge(DEFAULTS, settings)
    # L-1: owner-only NTFS ACL on Windows (the chmod below is a no-op there).
    secure_dir(SETTINGS_DIR)

    tmp_path = SETTINGS_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2)
    # Tighten perms to owner read/write only (best-effort; no-op semantics on
    # platforms that don't honor POSIX mode bits, but harmless there).
    try:
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        log.debug("chmod 0600 on settings failed: %s", exc)
    os.replace(tmp_path, SETTINGS_PATH)
    try:
        os.chmod(SETTINGS_PATH, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        log.debug("chmod 0600 on settings failed: %s", exc)
    restrict_to_current_user(SETTINGS_PATH)  # L-1: explicit owner-only ACL on Windows
