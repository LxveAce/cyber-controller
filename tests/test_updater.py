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

import json

import pytest

from src.core import flash_core, updater


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


# ---- latest_releases reads the metadata document under a fixed byte bound (U1) -------------------

class _FakeResponse:
    """A urllib-like response over bytes that honours read(n) and records every request."""

    def __init__(self, data):
        self.data, self.pos, self.requested = data, 0, []

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self.data) - self.pos
        self.requested.append(n)
        chunk = self.data[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serve(monkeypatch, data):
    """Serve *data* from the hardened opener; returns the responses handed out."""
    made = []

    def fake_open(req, timeout=None):
        resp = _FakeResponse(data)
        made.append(resp)
        return resp

    monkeypatch.setattr(flash_core._OPENER, "open", fake_open)
    return made


def _padded_list(total):
    """A valid one-release JSON list padded with whitespace (still valid JSON) to *total* bytes."""
    body = json.dumps([_rel("1.0.0")]).encode()
    assert len(body) < total
    return body + b" " * (total - len(body))


def test_latest_releases_makes_one_bounded_read(monkeypatch):
    made = _serve(monkeypatch, json.dumps([_rel("1.0.0")]).encode())
    assert updater.latest_releases(timeout=0.01)[0]["tag_name"] == "1.0.0"
    assert made[0].requested == [updater.MAX_RELEASES_BYTES + 1], "bound plus one, single read"


def test_latest_releases_accepts_a_valid_document_exactly_at_the_bound(monkeypatch):
    _serve(monkeypatch, _padded_list(updater.MAX_RELEASES_BYTES))
    assert updater.latest_releases(timeout=0.01)[0]["tag_name"] == "1.0.0"


def test_latest_releases_refuses_one_byte_over_the_bound_as_offline(monkeypatch):
    made = _serve(monkeypatch, _padded_list(updater.MAX_RELEASES_BYTES + 1))
    with pytest.raises(updater.UpdaterOffline, match="exceeds"):
        updater.latest_releases(timeout=0.01)
    assert made[0].requested == [updater.MAX_RELEASES_BYTES + 1]


def test_latest_releases_never_parses_an_oversized_document(monkeypatch):
    _serve(monkeypatch, b"[" + b" " * (updater.MAX_RELEASES_BYTES + 64))   # would be invalid JSON
    monkeypatch.setattr(updater.json, "loads",
                        lambda *a, **k: pytest.fail("an oversized document must not be parsed"))
    with pytest.raises(updater.UpdaterOffline, match="exceeds"):
        updater.latest_releases(timeout=0.01)


@pytest.mark.parametrize("data", [bytes([0xFF, 0xFE, 0xFD]), b"{not json", b'{"a": 1}', b""],
                         ids=["not-utf8", "malformed", "not-a-list", "empty"])
def test_latest_releases_refuses_bad_data_as_offline(monkeypatch, data):
    _serve(monkeypatch, data)
    with pytest.raises(updater.UpdaterOffline):
        updater.latest_releases(timeout=0.01)


def test_latest_releases_wraps_a_read_failure_as_offline(monkeypatch):
    class _Broken(_FakeResponse):
        def read(self, n=-1):
            raise OSError("connection reset mid-body")

    monkeypatch.setattr(flash_core._OPENER, "open", lambda req, timeout=None: _Broken(b""))
    with pytest.raises(updater.UpdaterOffline, match="connection reset") as info:
        updater.latest_releases(timeout=0.01)
    assert isinstance(info.value.__cause__, OSError)


# ---- release_page_url: the one boundary both UIs receive release links through (U2) -------------

_TAG_LINK = "https://github.com/LxveAce/cyber-controller/releases/tag/v2.0.1"


@pytest.mark.parametrize("url", [
    _TAG_LINK,
    "https://github.com/LxveAce/cyber-controller/releases/tag/2.0.0",
    "https://github.com/LxveAce/cyber-controller/releases/tag/v1.7.0-beta",
    "https://github.com/LxveAce/cyber-controller/releases/tag/v1.8.0_rc.1",
    updater.RELEASES_PAGE,
], ids=["published-tag", "bare-version", "prerelease-tag", "underscore-tag", "releases-page"])
def test_release_page_url_keeps_legitimate_links_unchanged(url):
    assert updater.release_page_url(url) == url


@pytest.mark.parametrize("url", [
    "http://github.com/LxveAce/cyber-controller/releases/tag/v2.0.1",
    "https://example.com/LxveAce/cyber-controller/releases/tag/v2.0.1",
    "https://github.com@example.com/LxveAce/cyber-controller/releases/tag/v2.0.1",
    "https://github.com:8443/LxveAce/cyber-controller/releases/tag/v2.0.1",
    "https://github.com.example.com/LxveAce/cyber-controller/releases/tag/v2.0.1",
    "https://github.com/Other/repo/releases/tag/v2.0.1",
    "https://github.com/LxveAce/cyber-controller/settings",
    "https://github.com/LxveAce/cyber-controller/releases/tag/v2.0.1/../../settings",
    "https://github.com/LxveAce/cyber-controller/releases/tag/v2.0.1?x=1",
    "https://github.com/LxveAce/cyber-controller/releases/tag/v2.0.1#frag",
    "https://github.com/LxveAce/cyber-controller/releases/tag/",
    "https://github.com/LxveAce/cyber-controller/releases/tag/..",
    "https://github.com/LxveAce/cyber-controller/releases/tag/v2.0.1\\evil",
    "https://github.com/LxveAce/cyber-controller/releases/tag/v2.0.1/extra",
    "javascript:alert(1)",
    "file:///etc/passwd",
    "not a url",
    "",
], ids=["http", "other-host", "userinfo", "port", "lookalike-host", "other-repo", "other-path",
        "traversal", "query", "fragment", "empty-tag", "dot-dot-tag", "backslash", "trailing-path",
        "javascript", "file", "garbage", "empty"])
def test_release_page_url_falls_back_for_unexpected_links(url):
    assert updater.release_page_url(url) == updater.RELEASES_PAGE


@pytest.mark.parametrize("value", [None, 42, 1.5, True, [_TAG_LINK], {"url": _TAG_LINK}, b"x"],
                         ids=["none", "int", "float", "bool", "list", "dict", "bytes"])
def test_release_page_url_handles_absent_or_non_string_metadata(value):
    assert updater.release_page_url(value) == updater.RELEASES_PAGE


def test_release_page_url_handles_a_malformed_authority_finitely():
    assert updater.release_page_url("https://[::1/LxveAce/cyber-controller/releases") \
        == updater.RELEASES_PAGE


def test_consumers_receive_only_the_validated_link(monkeypatch):
    # Both desktop call sites read apply_update_url(result), whose value _newest sets.
    _patch_releases(monkeypatch, [_rel("2.0.0", html_url="https://evil.example/LxveAce/x")])
    result = updater.check("1.0.0")
    assert result.status == updater.NEWER and result.latest_url == updater.RELEASES_PAGE
    assert updater.apply_update_url(result) == updater.RELEASES_PAGE


def test_consumers_keep_a_legitimate_tag_link(monkeypatch):
    _patch_releases(monkeypatch, [_rel("2.0.0")])
    result = updater.check("1.0.0")
    assert updater.apply_update_url(result) == \
        "https://github.com/LxveAce/cyber-controller/releases/tag/2.0.0"


def test_missing_html_url_falls_back_to_the_releases_page(monkeypatch):
    rel = _rel("2.0.0")
    del rel["html_url"]
    _patch_releases(monkeypatch, [rel])
    assert updater.apply_update_url(updater.check("1.0.0")) == updater.RELEASES_PAGE


# ---- release_page_url checks the original string before parsing -------------------------------

@pytest.mark.parametrize("url", [
    "\n" + _TAG_LINK,
    _TAG_LINK.replace("github.com", "git\thub.com"),
    _TAG_LINK.replace("v2.0.1", "v2.0\n.1"),
], ids=["leading-newline", "tab-in-host", "newline-in-tag"])
def test_release_page_url_refuses_the_control_characters_urlsplit_would_strip(url):
    assert updater.release_page_url(url) == updater.RELEASES_PAGE


def _with(char, where):
    if where == "start":
        return char + _TAG_LINK
    if where == "host":
        return _TAG_LINK.replace("github.com", "git" + char + "hub.com")
    if where == "tag":
        return _TAG_LINK.replace("v2.0.1", "v2.0" + char + ".1")
    return _TAG_LINK + char


@pytest.mark.parametrize("code", list(range(0x00, 0x20)) + [0x7F], ids=lambda c: f"0x{c:02x}")
@pytest.mark.parametrize("where", ["start", "host", "tag", "end"])
def test_release_page_url_refuses_every_ascii_control_anywhere(code, where):
    assert updater.release_page_url(_with(chr(code), where)) == updater.RELEASES_PAGE


@pytest.mark.parametrize("char", [" ", "\u00a0", "\u2028", "\u2029", "\u3000", "\ufeff"],
                         ids=["space", "nbsp", "line-sep", "para-sep", "ideographic-space", "bom"])
@pytest.mark.parametrize("where", ["start", "host", "tag", "end"])
def test_release_page_url_refuses_raw_whitespace_anywhere(char, where):
    assert updater.release_page_url(_with(char, where)) == updater.RELEASES_PAGE


@pytest.mark.parametrize("char", ["%", "@", "?", "#", "&", "=", "+", "~", "!", "'", "\"", "<", ">",
                                  "\u00e9", "\u0130"],
                         ids=["percent", "at", "question", "hash", "amp", "eq", "plus", "tilde",
                              "bang", "quote", "dquote", "lt", "gt", "e-acute", "dotted-I"])
def test_release_page_url_refuses_characters_outside_the_canonical_set(char):
    assert updater.release_page_url(_TAG_LINK.replace("v2.0.1", "v2" + char + ".0.1")) \
        == updater.RELEASES_PAGE


def test_release_page_url_never_returns_a_string_with_controls_or_whitespace():
    seen = set()
    for code in list(range(0x00, 0x21)) + [0x7F, 0xA0, 0x2028]:
        for where in ("start", "host", "tag", "end"):
            seen.add(updater.release_page_url(_with(chr(code), where)))
    assert seen == {updater.RELEASES_PAGE}
