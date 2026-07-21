"""Smart-installation / version-state handling.

Cyber Controller ships as a portable executable, so there is no classic installer — but it keeps
persistent state in ``~/.cyber-controller`` (settings, access-gate config, vault, audit trail, macros).
When a *different* version runs against existing state, this module decides what to do:

  * **fresh**    — no prior state → record the version, proceed.
  * **same**     — state from this version → proceed.
  * **upgrade**  — state from an older version → run any migrations, record the new version (silent;
                   settings deep-merge onto DEFAULTS and the AES-GCM vault carries its own format
                   version, so existing config stays usable).
  * **legacy**   — state exists but predates version tracking (pre-1.4) → treated as an upgrade.
  * **downgrade**— state from a *newer* version → genuinely risky (the newer version may have written
                   formats this build can't read). The GUI prompts: keep & continue, or back up & start
                   fresh. This is the "paths collide / overwrite old installation" case.

Pure logic + filesystem only (no Qt) so it is unit-testable; the GUI shows the downgrade prompt.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from src.version import __version__

log = logging.getLogger(__name__)

# Overridable for tests.
_CONFIG_DIR = Path.home() / ".cyber-controller"

# Files that mark a *real* existing install (not just an empty/auto-created dir).
_STATE_MARKERS = (
    "settings.json", "access_gate.json", "vault.hdr.json", "vault.dat",
    "audit-trail.jsonl", "web_secret.key",
)

# Migration registry: (version_boundary, callable). A migration runs when upgrading ACROSS its boundary
# (from < boundary <= to). Currently empty — settings deep-merge + vault versioning handle compatibility.
_MIGRATIONS: list[tuple[str, object]] = []


def config_dir() -> Path:
    return _CONFIG_DIR


def captures_dir() -> Path:
    """The one canonical folder for Wi-Fi captures: ``$CC_CAPTURES_DIR`` or ``~/.cyber-controller/captures``.

    WS-7 unifies the capture->crack workflow around a single place: a raw ``.pcap``/``.pcapng`` retrieved
    from a device, the ``.hc22000`` an auto-EAPOL convert writes, and Crack Lab's Browse default all point
    here — so a just-captured file is one click from cracking (and one click from a WiGLE/WGD upload later).
    Created on first use; falls back to the path even if the mkdir fails (a picker just opens elsewhere)."""
    env = os.environ.get("CC_CAPTURES_DIR")
    d = Path(env) if env else (_CONFIG_DIR / "captures")
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.debug("could not create captures dir %s", d, exc_info=True)
    return d


def _version_file() -> Path:
    return _CONFIG_DIR / ".installed_version"


def has_existing_state() -> bool:
    d = _CONFIG_DIR
    if not d.is_dir():
        return False
    return any((d / m).exists() for m in _STATE_MARKERS)


def installed_version() -> str | None:
    """The version that last wrote the config dir, or None if unrecorded (fresh or pre-1.4 'legacy')."""
    f = _version_file()
    try:
        if f.exists():
            return f.read_text(encoding="utf-8").strip() or None
    except Exception:
        log.debug("could not read installed-version marker", exc_info=True)
    return None


def record_version(v: str = __version__) -> None:
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _version_file().write_text(v, encoding="utf-8")
    except Exception:
        log.debug("could not record installed version", exc_info=True)


def _parse(v: str | None) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums[:3]) or (0,)


def classify(current: str = __version__, stored: str | object = "__auto__") -> str:
    """Return one of: ``fresh`` | ``legacy`` | ``same`` | ``upgrade`` | ``downgrade``."""
    if stored == "__auto__":
        stored = installed_version()
    if not has_existing_state():
        return "fresh"
    if stored is None:
        return "legacy"
    c, s = _parse(current), _parse(stored)  # type: ignore[arg-type]
    if c == s:
        return "same"
    return "upgrade" if c > s else "downgrade"


def backup_config_dir() -> Path | None:
    """Move the existing config dir aside (timestamped) so the app starts fresh. Returns the backup path.

    This is the safe "overwrite the old installation" action — nothing is destroyed, just renamed, so the
    user can restore it by moving it back.
    """
    d = _CONFIG_DIR
    if not d.is_dir():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bk = d.with_name(d.name + ".bak." + stamp)
    i = 1
    while bk.exists():
        bk = d.with_name(f"{d.name}.bak.{stamp}-{i}")
        i += 1
    try:
        shutil.move(str(d), str(bk))
        log.info("backed up existing config to %s", bk)
        return bk
    except Exception:
        log.exception("config backup failed")
        return None


def run_migrations(from_v: str | None, to_v: str = __version__) -> list[str]:
    applied: list[str] = []
    for ver, fn in _MIGRATIONS:
        if _parse(from_v) < _parse(ver) <= _parse(to_v):
            try:
                fn()  # type: ignore[operator]
                applied.append(ver)
            except Exception:
                log.exception("migration %s failed", ver)
    return applied


def reconcile(current: str = __version__) -> str:
    """Startup reconciliation safe for ALL UIs (and headless): classify, migrate-on-upgrade, record.

    Records the version for fresh/same/upgrade/legacy. For a **downgrade** it logs a warning and does
    NOT overwrite the newer marker — the GUI calls :func:`classify` again and prompts the user. Returns
    the classification.
    """
    prior = installed_version()
    status = classify(current)
    if status in ("upgrade", "legacy"):
        run_migrations(prior, current)
        log.info("Cyber Controller config carried forward to v%s (was %s)", current, prior or "pre-1.4")
        record_version(current)
    elif status in ("fresh", "same"):
        record_version(current)
    elif status == "downgrade":
        log.warning(
            "Existing config was written by a newer version (v%s) than this build (v%s); "
            "the GUI will offer to keep it or back up and start fresh.", prior, current,
        )
    return status
