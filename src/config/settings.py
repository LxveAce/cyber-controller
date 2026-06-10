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

log = logging.getLogger(__name__)


# ── Defaults ─────────────────────────────────────────────────────────

DEFAULTS: dict[str, dict[str, Any]] = {
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
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)

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
