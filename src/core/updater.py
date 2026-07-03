"""In-app update check — pure logic + a single hardened network fetch, NO PyQt.

Phase 1 is **deep-link only**: this module never downloads or self-updates. It asks GitHub for the
list of published releases, decides whether the running build is behind, and (for the caller) hands
back the release page URL to open in a browser. Everything here is import-safe headless and
unit-testable — the Qt dialogs + wiring live in ``src/ui/qt/update_dialog.py`` and the main window.

Design notes
------------
* The network fetch reuses the SSRF-hardened opener + allowlist from :mod:`src.core.flash_core`
  (``api.github.com`` is already on that allowlist), so a redirect can never point the fetch off the
  trusted GitHub host set. Any network/parse failure is folded into a clean :class:`UpdaterOffline`.
* Version parsing/compare reuses :func:`src.core.install._parse` (regex over the digit groups), so a
  ``v`` prefix or extra suffix is tolerated: ``v2.0.0`` == ``2.0``.
* :func:`should_prompt` is a PURE decision so it can be table-tested exhaustively with no network.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from src.core import flash_core, install

log = logging.getLogger(__name__)

# The releases LIST endpoint (newest first). We read the whole list — not /latest — so we can count
# how many published releases are strictly newer than the running build.
RELEASES_API = "https://api.github.com/repos/LxveAce/cyber-controller/releases"
# Fallback deep-link when a specific release carries no html_url.
RELEASES_PAGE = "https://github.com/LxveAce/cyber-controller/releases"

# Hard, short default timeout so the check never lingers (it also runs off the UI thread).
DEFAULT_TIMEOUT = 6.0

# Result statuses.
UP_TO_DATE = "UP_TO_DATE"
NEWER = "NEWER"
OFFLINE = "OFFLINE"


class UpdaterOffline(Exception):
    """Raised by :func:`latest_releases` on ANY network/parse failure (treated as 'offline')."""


@dataclass
class CheckResult:
    """Outcome of a version check. ``latest_tag``/``latest_url`` are '' when unknown/offline."""

    status: str                    # UP_TO_DATE | NEWER | OFFLINE
    latest_tag: str = ""
    latest_url: str = ""
    behind: int = 0
    tags: list[str] = field(default_factory=list)


def now_iso() -> str:
    """UTC ISO-8601 timestamp (seconds precision) for last_check_iso bookkeeping."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def latest_releases(timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    """GET the GitHub releases list for the repo. Raise :class:`UpdaterOffline` on any failure.

    Reuses flash_core's SSRF guard + redirect-allowlisted opener so the fetch (and any redirect)
    can only ever reach the trusted GitHub host set.
    """
    try:
        flash_core._require_allowed_url(RELEASES_API)
        req = urllib.request.Request(RELEASES_API, headers=flash_core._UA)
        with flash_core._OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — any failure is "offline" for our purposes
        raise UpdaterOffline(str(exc)) from exc
    if not isinstance(data, list):
        raise UpdaterOffline("unexpected releases payload (not a list)")
    return data


def _published_releases(releases: list[dict]) -> list[dict]:
    """Filter out drafts (never public) and prereleases (not an offered stable update)."""
    out: list[dict] = []
    for rel in releases:
        if not isinstance(rel, dict):
            continue
        if rel.get("draft") or rel.get("prerelease"):
            continue
        if rel.get("tag_name"):
            out.append(rel)
    return out


def release_tags(releases: list[dict]) -> list[str]:
    """Tag strings of the published (non-draft, non-prerelease) releases."""
    return [str(r["tag_name"]) for r in _published_releases(releases)]


def behind_count(installed: str, tags: list[str]) -> int:
    """Number of tags strictly newer than *installed* (tolerant of 'v' prefixes / suffixes)."""
    iv = install._parse(installed)
    return sum(1 for t in tags if install._parse(t) > iv)


def _newest(releases: list[dict]) -> tuple[str, str]:
    """Return (tag, html_url) of the newest published release, or ('', '') if none."""
    best_rel: dict | None = None
    best_ver: tuple[int, ...] | None = None
    for rel in _published_releases(releases):
        ver = install._parse(str(rel.get("tag_name")))
        if best_ver is None or ver > best_ver:
            best_ver, best_rel = ver, rel
    if best_rel is None:
        return "", ""
    return str(best_rel.get("tag_name") or ""), str(best_rel.get("html_url") or RELEASES_PAGE)


def check(installed: str, settings_updates: Mapping[str, Any] | None = None,
          timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Perform the network check and classify the result.

    Returns a :class:`CheckResult` with status OFFLINE on any network failure, NEWER when at least
    one published release is ahead, else UP_TO_DATE. This is network + classification only — the
    prompt/suppression decision is the pure :func:`should_prompt`. ``settings_updates`` is accepted
    for a stable call signature but does not influence the network result.
    """
    try:
        releases = latest_releases(timeout)
    except UpdaterOffline as exc:
        log.debug("update check offline: %s", exc)
        return CheckResult(status=OFFLINE)
    tags = release_tags(releases)
    behind = behind_count(installed, tags)
    latest_tag, latest_url = _newest(releases)
    status = NEWER if behind >= 1 else UP_TO_DATE
    return CheckResult(status=status, latest_tag=latest_tag, latest_url=latest_url,
                       behind=behind, tags=tags)


def should_prompt(state: Mapping[str, Any], behind: int) -> bool:
    """PURE decision: should we show the update-available prompt for *behind* releases?

    Rules (literal to spec):
      * Never prompt when ``behind < 1``.
      * OVERRIDE any suppression and prompt when ``behind >= 2 AND behind > suppressed_at_behind``
        (a genuinely newer release arrived after the user dismissed an earlier one).
      * Otherwise prompt UNLESS the user suppressed AND we are still only one behind AND that one is
        no newer than what they dismissed: ``suppressed AND behind < 2 AND behind <= suppressed_at_behind``.
    """
    if behind < 1:
        return False
    suppressed_at = int(state.get("suppressed_at_behind", 0) or 0)
    if behind >= 2 and behind > suppressed_at:
        return True
    suppressed = bool(state.get("suppressed", False))
    if suppressed and behind < 2 and behind <= suppressed_at:
        return False
    return True


def should_auto_check(state: Mapping[str, Any], force: bool = False) -> bool:
    """PURE gate for the AUTOMATIC startup check. A manual (force) check always runs; otherwise the
    check runs only when ``updates.enabled`` is true. Suppression NEVER gates the check itself — only
    the prompt (see :func:`should_prompt`)."""
    if force:
        return True
    return bool(state.get("enabled", True))


def apply_update_url(result: CheckResult) -> str:
    """The URL the caller should open to 'apply' an update (phase 1 = deep-link to the release page)."""
    return result.latest_url or RELEASES_PAGE
