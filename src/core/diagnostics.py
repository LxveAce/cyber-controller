"""In-app diagnostics and bug-report bundles.

A ring buffer quietly captures recent log records for the whole session; ``collect_report`` assembles a
REDACTED text bundle (version, platform, recent logs, plus the user's note) that the user can save, copy,
or attach to a GitHub issue and "send back for fixing". Redaction strips GitHub tokens, Bearer headers,
emails, key=value secrets, and — importantly for a tool run from a user profile — the home path and the
OS username, so a shared report doesn't leak PII.
"""

from __future__ import annotations

import getpass
import logging
import os
import platform
import re
import sys
import urllib.parse
from collections import deque
from datetime import datetime, timezone

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_ISSUE_BASE = "https://github.com/LxveAce/cyber-controller/issues/new"

_RING: "RingLogHandler | None" = None


class RingLogHandler(logging.Handler):
    """A logging handler that keeps the most recent formatted records in a fixed-size ring."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter(_LOG_FORMAT))
        self._buf: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buf.append(self.format(record))
        except Exception:  # noqa: BLE001 — a logging handler must never raise
            pass

    def records(self) -> list[str]:
        return list(self._buf)

    def clear(self) -> None:
        self._buf.clear()


def install_ring_handler(capacity: int = 500, level: int = logging.INFO) -> RingLogHandler:
    """Attach the process-wide ring handler to the root logger (idempotent)."""
    global _RING
    if _RING is None:
        _RING = RingLogHandler(capacity)
        _RING.setLevel(level)
        logging.getLogger().addHandler(_RING)
    return _RING


def get_ring_handler() -> "RingLogHandler | None":
    return _RING


# ── Redaction ────────────────────────────────────────────────────────

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"gh[posru]_[A-Za-z0-9]{20,}"), "<github-token>"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "<github-token>"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"), "Bearer <redacted>"),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"(?i)\b(token|password|passwd|secret|api[_-]?key)\b\s*[=:]\s*\S+"), r"\1=<redacted>"),
    # MAC / BSSID. A scanned BSSID is directly geolocatable (WiGLE), so a diagnostics bundle that leaks
    # one discloses the user's physical location. (SSIDs are arbitrary strings and can't be regex-matched
    # safely — those are kept out of the INFO ring at the log source in cross_comm instead.)
    (re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"), "<mac>"),
]


def _redact_paths(text: str) -> str:
    """Replace the user's home directory with ~ and the OS username with <user>."""
    home = os.path.expanduser("~")
    for variant in {home, home.replace("\\", "/"), home.replace("/", "\\")}:
        if variant and variant != "~":
            text = text.replace(variant, "~")
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if user and len(user) >= 3:
        text = re.sub(rf"(?<![\w]){re.escape(user)}(?![\w])", "<user>", text)
    return text


def redact(text: str) -> str:
    """Scrub secrets and PII from a diagnostics string. Safe on empty input."""
    if not text:
        return text
    text = _redact_paths(text)
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


# ── Report assembly ──────────────────────────────────────────────────

def collect_report(
    user_note: str = "",
    *,
    include_logs: bool = True,
    max_log_lines: int = 200,
    extra: dict | None = None,
) -> str:
    """Assemble the full, redacted bug-report text."""
    try:
        from src.version import __version__
    except Exception:  # noqa: BLE001
        __version__ = "unknown"

    lines = [
        "# Cyber Controller bug report",
        f"generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}",
        f"version: {__version__}",
        f"platform: {platform.platform()}",
        f"python: {platform.python_version()}",
        f"frozen: {bool(getattr(sys, 'frozen', False))}",
    ]
    for key, val in (extra or {}).items():
        lines.append(f"{key}: {val}")
    lines += ["", "## What happened", (user_note or "").strip() or "(no description provided)"]

    if include_logs:
        handler = get_ring_handler()
        recs = handler.records() if handler else []
        lines += ["", "## Recent logs (most recent last)"]
        lines.append("\n".join(recs[-max_log_lines:]) if recs else "(no logs captured)")

    return redact("\n".join(lines))


def github_issue_url(title: str, body: str, *, max_body: int = 6000) -> str:
    """Build a prefilled GitHub 'new issue' URL. Body is truncated to keep the URL under GitHub's cap.

    The title is redacted here too: callers derive it from the user's raw note, and a prefilled title
    lands in a PUBLIC, search-indexed issue — an un-redacted email or token there would defeat the whole
    point of redacting the body.
    """
    if len(body) > max_body:
        body = body[:max_body] + "\n… (truncated — attach the full report file)"
    safe_title = redact(title or "Bug report")[:120] or "Bug report"
    query = urllib.parse.urlencode({"title": safe_title, "body": body})
    return f"{_ISSUE_BASE}?{query}"
