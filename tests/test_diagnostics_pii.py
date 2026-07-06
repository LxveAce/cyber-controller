"""Diagnostics redaction must scrub a BSSID (WiGLE-geolocatable → leaks physical location) and must
redact the GitHub-issue TITLE, not just the body — the title lands in a public, search-indexed issue.
"""

from __future__ import annotations

import urllib.parse

from src.core.diagnostics import github_issue_url, redact


def test_redact_scrubs_mac_bssid_colon_and_dash():
    assert "AA:BB:CC:DD:EE:FF" not in redact("AutoRouter matched target ap:AA:BB:CC:DD:EE:FF")
    assert "<mac>" in redact("ap:AA:BB:CC:DD:EE:FF")
    assert "<mac>" in redact("bssid a0-b1-c2-d3-e4-f5 seen")


def test_redact_leaves_plain_hex_alone():
    # A hex blob without the MAC colon/dash structure must not be mangled by the MAC rule.
    assert redact("sha256 deadbeef00112233445566") == "sha256 deadbeef00112233445566"


def test_issue_title_is_redacted():
    tok = "ghp_" + "A" * 30
    url = github_issue_url(f"Bug: email me at foo@bar.com token {tok}", "body")
    title = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["title"][0]
    assert "foo@bar.com" not in title and "<email>" in title
    assert "ghp_" not in title and "<github-token>" in title


def test_issue_title_falls_back_when_empty():
    url = github_issue_url("", "body")
    title = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["title"][0]
    assert title == "Bug report"
