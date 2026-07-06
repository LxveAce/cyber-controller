"""Tests for ``src.core.diagnostics`` — ring log buffer, redaction, and bug-report assembly.

No Qt, no network. Verifies the report that gets "sent back for fixing" never carries tokens/PII.
"""

from __future__ import annotations

import getpass
import logging
import os

import pytest

diagnostics = pytest.importorskip("src.core.diagnostics")
from src.core.diagnostics import (  # noqa: E402
    RingLogHandler,
    collect_report,
    github_issue_url,
    install_ring_handler,
    redact,
)


def test_ring_handler_keeps_last_n():
    h = RingLogHandler(capacity=3)
    rec = lambda m: logging.LogRecord("t", logging.INFO, __file__, 1, m, None, None)
    for m in ("a", "b", "c", "d"):
        h.emit(rec(m))
    recs = h.records()
    assert len(recs) == 3
    assert recs[0].endswith("b") and recs[-1].endswith("d")  # 'a' dropped
    h.clear()
    assert h.records() == []


def test_install_ring_handler_idempotent():
    h1 = install_ring_handler()
    h2 = install_ring_handler()
    assert h1 is h2
    assert h1 in logging.getLogger().handlers


def test_redact_tokens_and_secrets():
    out = redact("token gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123 and Authorization: Bearer abc.def-123")
    assert "gho_ABCDEF" not in out
    assert "<github-token>" in out
    assert "Bearer <redacted>" in out
    assert redact("password = hunter2").endswith("<redacted>")


def test_redact_email_and_pii():
    out = redact("contact me at someone@example.com please")
    assert "someone@example.com" not in out and "<email>" in out


def test_redact_home_path_and_username():
    home = os.path.expanduser("~")
    user = getpass.getuser()
    sample = f"failed reading {home}\\config for {user}"
    out = redact(sample)
    assert home not in out and "~" in out
    if len(user) >= 3:
        assert user not in out and "<user>" in out


def test_collect_report_has_version_and_is_redacted():
    install_ring_handler()
    logging.getLogger("test.diag").info("using gho_SECRETTOKEN1234567890ABCD now")
    report = collect_report("it broke when I flashed", extra={"connected_devices": 2})
    assert "# Cyber Controller bug report" in report
    assert "version:" in report
    assert "it broke when I flashed" in report
    assert "connected_devices: 2" in report
    assert "gho_SECRETTOKEN" not in report  # ring log line redacted in the bundle


def test_github_issue_url_encodes_and_truncates():
    url = github_issue_url("Bug: flash fails", "x" * 9000)
    assert url.startswith("https://github.com/LxveAce/cyber-controller/issues/new?")
    assert "title=Bug" in url.replace("%3A", ":").replace("+", " ") or "title=" in url
    assert "truncated" in url  # body over cap gets a truncation marker
