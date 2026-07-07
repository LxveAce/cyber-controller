"""Regression tests for :mod:`scripts.vt_release_scan`'s retry loop.

The VirusTotal public API returns HTTP 429 (4 req/min rate limit) very readily, so ``api()``
retries. Two coupled defects used to make those retries wrong:

  1. ``kw.pop("timeout", ...)`` mutated ``kw`` on the first attempt, so every retry silently lost
     the caller's timeout and fell back to the 120s default.
  2. The upload path in ``stats_for`` handed a single file handle to ``api``; when the POST hit a
     429, the retry re-sent the SAME handle already positioned at EOF, so VirusTotal analysed an
     EMPTY file and reported a bogus detection ratio.

These tests drive ``api``/``stats_for`` with a fake ``requests.request`` that forces one 429 before
success, and assert the retry keeps the timeout and re-sends the full file bytes. Both FAIL against
the pre-fix code and PASS after it. No network is touched.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# scripts/ is not an importable package, so load the module straight from its file.
_MOD_PATH = Path(__file__).resolve().parent.parent / "scripts" / "vt_release_scan.py"
_spec = importlib.util.spec_from_file_location("vt_release_scan", _MOD_PATH)
vt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vt)


class _Resp:
    """Minimal stand-in for a ``requests.Response``."""

    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Neutralise the 35s back-off / 20s / 16s sleeps so tests run instantly."""
    monkeypatch.setattr(vt.time, "sleep", lambda *_a, **_k: None)


# ── defect 2: retry must keep the caller-supplied timeout ─────────────────────────────────────────

def test_api_retry_keeps_caller_timeout(monkeypatch):
    seen_timeouts: list = []

    calls = {"n": 0}

    def fake_request(method, url, headers=None, timeout=None, **kw):
        seen_timeouts.append(timeout)
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(429)  # force exactly one retry
        return _Resp(200, {"ok": True})

    monkeypatch.setattr(vt.requests, "request", fake_request)

    r = vt.api("GET", "https://vt.example/x", "KEY", timeout=600)

    assert r.status_code == 200
    assert seen_timeouts == [600, 600], (
        "retry lost the caller's timeout (fell back to the 120s default)"
    )


# ── defect 1: 429 retry must re-send the full file, not an EOF-positioned handle ───────────────────

def test_stats_for_reuploads_full_file_after_rate_limit(monkeypatch, tmp_path):
    content = b"REAL-BINARY-CONTENT-" * 100  # non-empty payload we expect on every attempt
    binpath = tmp_path / "cyber-controller-linux-x64"
    binpath.write_bytes(content)

    posted_bodies: list[bytes] = []
    post_calls = {"n": 0}

    def fake_request(method, url, headers=None, timeout=None, **kw):
        if method == "GET" and url.endswith("/files/upload_url"):
            return _Resp(200, {"data": "https://upload.example/slot"})
        if method == "GET" and "/files/" in url:
            return _Resp(404)  # not seen by VT yet -> take the upload branch
        if method == "POST":
            # Read whatever the multipart body carries THIS attempt, exactly as requests would.
            fh = kw["files"]["file"][1]
            posted_bodies.append(fh.read())
            post_calls["n"] += 1
            if post_calls["n"] == 1:
                return _Resp(429)  # rate-limit the first upload attempt
            return _Resp(200, {"data": {"id": "analysis-xyz"}})
        if method == "GET" and "/analyses/" in url:
            return _Resp(200, {"data": {"attributes": {
                "status": "completed",
                "stats": {"malicious": 0, "suspicious": 0, "undetected": 70, "harmless": 5},
            }}})
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(vt.requests, "request", fake_request)

    sha, stats = vt.stats_for(str(binpath), "KEY")

    assert post_calls["n"] == 2, "expected the POST to be retried once after the 429"
    # The retry (second attempt) must carry the FULL file, not 0 bytes read past EOF.
    assert posted_bodies == [content, content], (
        f"retry re-sent {len(posted_bodies[1]) if len(posted_bodies) > 1 else 'no'} bytes; "
        "the upload stream was not rewound before the retry"
    )
    assert stats == {"malicious": 0, "suspicious": 0, "undetected": 70, "harmless": 5}
