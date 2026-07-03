"""Headless, fully-offline tests for the in-app updater core (src/core/updater.py).

Every network call is mocked — no test touches the real GitHub. Covers:
  * should_prompt truth table (incl. the 1.0 -> 2.0 -> 3.0 re-arm example),
  * behind_count with 'v' prefixes and mixed ordering,
  * check() classification for NEWER / UP_TO_DATE / OFFLINE,
  * the silent check ALWAYS runs (suppression never gates the check, only the prompt),
  * offline suppression is SEPARATE from version suppression,
  * should_auto_check enabled/disabled gate,
  * behind == 0 / 1 / 2+ boundaries.
"""

from __future__ import annotations

import pytest

from src.core import updater


def _rel(tag: str, *, prerelease: bool = False, draft: bool = False,
         html_url: str | None = None) -> dict:
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "draft": draft,
        "html_url": html_url or f"https://github.com/LxveAce/cyber-controller/releases/tag/{tag}",
    }


# ── behind_count (tolerant of 'v' prefixes) ──────────────────────────
@pytest.mark.parametrize("installed, tags, expected", [
    ("1.0.0", [], 0),
    ("1.0.0", ["1.0.0"], 0),
    ("1.0.0", ["v1.0.0"], 0),                       # v-prefix, equal
    ("1.0.0", ["v2.0.0"], 1),                       # v-prefix, newer
    ("v1.0.0", ["2.0.0"], 1),                       # installed has v-prefix
    ("1.0.0", ["2.0.0", "3.0.0"], 2),               # two ahead
    ("1.0.0", ["v2.0.0", "v3.0.0", "v1.0.0"], 2),   # v-prefixes + an equal (not counted)
    ("1.5.0", ["1.4.0", "1.5.0", "1.6.0"], 1),      # only 1.6 is newer
    ("2.0.0", ["1.0.0", "1.9.0"], 0),               # all older
])
def test_behind_count(installed, tags, expected):
    assert updater.behind_count(installed, tags) == expected


# ── should_prompt truth table ────────────────────────────────────────
# state = (suppressed, suppressed_at_behind)
@pytest.mark.parametrize("suppressed, at_behind, behind, expected", [
    # behind == 0 -> never prompt, regardless of state
    (False, 0, 0, False),
    (True, 5, 0, False),
    # behind == 1
    (False, 0, 1, True),    # not suppressed -> prompt
    (True, 1, 1, False),    # suppressed at this exact behind -> silent
    (True, 2, 1, False),    # suppressed, behind <= at -> silent
    (True, 0, 1, True),     # suppressed but at=0 (1 > 0) -> prompt
    (False, 3, 1, True),    # not suppressed -> prompt even with stale at
    # behind == 2 (override territory)
    (False, 0, 2, True),    # not suppressed -> prompt
    (True, 1, 2, True),     # override: 2 > 1 -> prompt again (the re-arm example)
    (True, 2, 2, True),     # not override (2 !> 2) but silence needs behind<2 -> prompt
    (True, 5, 2, True),     # behind>=2 never silenced (silence requires behind<2)
    # behind == 3+
    (True, 1, 3, True),     # override: 3 > 1
    (True, 3, 3, True),     # behind>=2 never silenced
])
def test_should_prompt(suppressed, at_behind, behind, expected):
    state = {"suppressed": suppressed, "suppressed_at_behind": at_behind}
    assert updater.should_prompt(state, behind) is expected


def test_should_prompt_narrative_1_0_to_2_0_to_3_0():
    """The canonical example: on 1.0, dismiss at behind=1 -> a further release re-arms the prompt."""
    installed = "1.0.0"
    fresh = {"suppressed": False, "suppressed_at_behind": 0}

    # 2.0 is out -> behind 1 -> not suppressed -> prompt.
    b1 = updater.behind_count(installed, ["2.0.0"])
    assert b1 == 1
    assert updater.should_prompt(fresh, b1) is True

    # User dismisses with "don't show again": suppressed at behind=1.
    dismissed = {"suppressed": True, "suppressed_at_behind": b1, "dismissed_version": "2.0.0"}

    # Still only 2.0 out -> behind 1 -> silenced.
    assert updater.should_prompt(dismissed, updater.behind_count(installed, ["2.0.0"])) is False

    # 3.0 releases -> behind 2 (2.0 + 3.0 both newer) -> 2 > 1 -> prompt again.
    b2 = updater.behind_count(installed, ["2.0.0", "3.0.0"])
    assert b2 == 2
    assert updater.should_prompt(dismissed, b2) is True


# ── should_auto_check enabled/disabled gate ──────────────────────────
@pytest.mark.parametrize("state, force, expected", [
    ({"enabled": True}, False, True),
    ({"enabled": False}, False, False),   # disabled -> no automatic check
    ({"enabled": False}, True, True),     # manual check bypasses disabled
    ({}, False, True),                    # default (missing key) -> enabled
    ({"enabled": True, "suppressed": True}, False, True),   # suppression never gates the check
])
def test_should_auto_check(state, force, expected):
    assert updater.should_auto_check(state, force=force) is expected


# ── check() classification (network mocked) ──────────────────────────
def _patch_releases(monkeypatch, releases):
    monkeypatch.setattr(updater, "latest_releases", lambda timeout=updater.DEFAULT_TIMEOUT: releases)


def test_check_up_to_date(monkeypatch):
    _patch_releases(monkeypatch, [_rel("1.5.0"), _rel("1.4.0")])
    result = updater.check("1.5.0")
    assert result.status == updater.UP_TO_DATE
    assert result.behind == 0
    assert result.latest_tag == "1.5.0"


def test_check_newer_single(monkeypatch):
    _patch_releases(monkeypatch, [_rel("2.0.0"), _rel("1.5.0")])
    result = updater.check("1.5.0")
    assert result.status == updater.NEWER
    assert result.behind == 1
    assert result.latest_tag == "2.0.0"
    assert result.latest_url.endswith("/tag/2.0.0")
    assert updater.apply_update_url(result).endswith("/tag/2.0.0")


def test_check_newer_multiple(monkeypatch):
    _patch_releases(monkeypatch, [_rel("v3.0.0"), _rel("v2.0.0"), _rel("v1.0.0")])
    result = updater.check("1.0.0")
    assert result.status == updater.NEWER
    assert result.behind == 2
    assert result.latest_tag == "v3.0.0"


def test_check_ignores_drafts_and_prereleases(monkeypatch):
    _patch_releases(monkeypatch, [
        _rel("3.0.0", draft=True),        # not public
        _rel("2.5.0", prerelease=True),   # not a stable offer
        _rel("2.0.0"),
        _rel("1.0.0"),
    ])
    result = updater.check("1.0.0")
    assert result.status == updater.NEWER
    assert result.behind == 1          # only 2.0.0 counts
    assert result.latest_tag == "2.0.0"


def test_check_offline_on_network_failure(monkeypatch):
    def _boom(timeout=updater.DEFAULT_TIMEOUT):
        raise updater.UpdaterOffline("no network")
    monkeypatch.setattr(updater, "latest_releases", _boom)
    result = updater.check("1.5.0")
    assert result.status == updater.OFFLINE
    assert result.behind == 0
    assert result.latest_tag == ""


def test_check_offline_on_unexpected_payload(monkeypatch):
    # latest_releases raises UpdaterOffline for a non-list payload; simulate that here.
    def _bad(timeout=updater.DEFAULT_TIMEOUT):
        raise updater.UpdaterOffline("unexpected releases payload (not a list)")
    monkeypatch.setattr(updater, "latest_releases", _bad)
    assert updater.check("1.0.0").status == updater.OFFLINE


# ── independence: the silent check always runs; suppression is separate ──
def test_check_runs_regardless_of_suppression(monkeypatch):
    """A suppressed version state does NOT stop check() from reporting NEWER — only the prompt is gated."""
    _patch_releases(monkeypatch, [_rel("2.0.0"), _rel("1.0.0")])
    suppressed_state = {"suppressed": True, "suppressed_at_behind": 1, "offline_error_suppressed": True}
    result = updater.check("1.0.0", suppressed_state)
    assert result.status == updater.NEWER          # the check ran and found the newer release
    assert result.behind == 1
    # The gating happens only at the prompt layer — and here it would be silent.
    assert updater.should_prompt(suppressed_state, result.behind) is False


def test_offline_suppression_separate_from_version_suppression():
    """should_prompt must ignore offline_error_suppressed entirely."""
    # offline suppressed but version NOT suppressed -> version prompt still fires.
    state = {"suppressed": False, "suppressed_at_behind": 0, "offline_error_suppressed": True}
    assert updater.should_prompt(state, 1) is True
    # version suppressed but offline NOT suppressed -> version prompt silenced (offline flag irrelevant).
    state2 = {"suppressed": True, "suppressed_at_behind": 1, "offline_error_suppressed": False}
    assert updater.should_prompt(state2, 1) is False


# ── network fetch is SSRF-guarded (uses flash_core's allowlisted opener) ──
def test_latest_releases_wraps_errors_as_offline(monkeypatch):
    """Any failure inside the fetch surfaces as UpdaterOffline, never a raw exception."""
    from src.core import flash_core

    def _explode(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(flash_core._OPENER, "open", _explode)
    with pytest.raises(updater.UpdaterOffline):
        updater.latest_releases(timeout=0.01)


def test_releases_api_targets_allowlisted_host():
    """The releases endpoint must pass flash_core's SSRF allowlist (api.github.com)."""
    from src.core import flash_core
    # Raises ValueError if not https + allowlisted host; returns the url on success.
    assert flash_core._require_allowed_url(updater.RELEASES_API) == updater.RELEASES_API
