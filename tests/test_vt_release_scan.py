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


# ── silent-failure defect: real VT failures must NOT be coerced into 'scan pending' ────────────────
#
# ``stats_for`` used to fall through to ``return sha, None`` for EVERY non-200/404 status, and
# ``api`` used to ``return r`` after exhausting its 429 retries. So an expired/wrong key (401/403),
# a 5xx outage, or persistent rate-limiting all produced ``(sha, None)`` — rendered as an innocuous
# ``_scan pending_`` row while the release still exited 0. These tests pin the fix: a genuine failure
# raises ``VTError`` (loud), while a legitimately-still-running analysis stays ``None`` (pending).


def test_api_raises_on_persistent_rate_limit(monkeypatch):
    """8 straight 429s is exhaustion, not success — api() must raise, not return the 429 response."""
    def fake_request(method, url, headers=None, timeout=None, **kw):
        return _Resp(429)

    monkeypatch.setattr(vt.requests, "request", fake_request)

    with pytest.raises(vt.VTError):
        vt.api("GET", "https://vt.example/x", "KEY")


def test_stats_for_raises_on_auth_error(monkeypatch, tmp_path):
    """A 401 from a wrong/expired key is a real failure — must raise, not return (sha, None)."""
    binpath = tmp_path / "cyber-controller-linux-x64"
    binpath.write_bytes(b"BINARY")

    def fake_request(method, url, headers=None, timeout=None, **kw):
        return _Resp(401)  # bad/expired/under-permissioned key

    monkeypatch.setattr(vt.requests, "request", fake_request)

    with pytest.raises(vt.VTError):
        vt.stats_for(str(binpath), "BAD-KEY")


def test_stats_for_raises_when_poll_errors(monkeypatch, tmp_path):
    """A 404 takes the upload path; if the analysis POLL then errors (5xx), that is a failure,
    not a pending scan — the loop must raise rather than spin out and return None."""
    binpath = tmp_path / "cyber-controller-linux-x64"
    binpath.write_bytes(b"FRESH-BINARY")

    def fake_request(method, url, headers=None, timeout=None, **kw):
        if method == "GET" and url.endswith("/files/upload_url"):
            return _Resp(200, {"data": "https://upload.example/slot"})
        if method == "GET" and "/files/" in url:
            return _Resp(404)  # not seen yet -> upload branch
        if method == "POST":
            return _Resp(200, {"data": {"id": "analysis-xyz"}})
        if method == "GET" and "/analyses/" in url:
            return _Resp(500)  # backend hiccup while polling
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(vt.requests, "request", fake_request)

    with pytest.raises(vt.VTError):
        vt.stats_for(str(binpath), "KEY")


def test_stats_for_returns_pending_when_analysis_still_running(monkeypatch, tmp_path):
    """The one legitimate None: upload succeeded but the analysis never reaches 'completed'
    within the poll window. This must stay a genuine 'pending' (None), not raise."""
    binpath = tmp_path / "cyber-controller-linux-x64"
    binpath.write_bytes(b"FRESH-BINARY")

    def fake_request(method, url, headers=None, timeout=None, **kw):
        if method == "GET" and url.endswith("/files/upload_url"):
            return _Resp(200, {"data": "https://upload.example/slot"})
        if method == "GET" and "/files/" in url:
            return _Resp(404)
        if method == "POST":
            return _Resp(200, {"data": {"id": "analysis-xyz"}})
        if method == "GET" and "/analyses/" in url:
            return _Resp(200, {"data": {"attributes": {"status": "queued"}}})  # never completes
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(vt.requests, "request", fake_request)

    sha, stats = vt.stats_for(str(binpath), "KEY")
    assert stats is None, "a still-running analysis is legitimately pending, not a failure"


class _FakeCompleted:
    def __init__(self, stdout=""):
        self.stdout = stdout


def test_main_fails_loudly_on_scan_error(monkeypatch, tmp_path):
    """End-to-end: an auth failure must make main() exit nonzero and write a 'scan FAILED' row —
    NOT publish a '_scan pending_' section and return 0."""
    bindir = tmp_path / "dist"
    bindir.mkdir()
    (bindir / "cyber-controller-linux-x64").write_bytes(b"BINARY-CONTENT")

    def fake_request(method, url, headers=None, timeout=None, **kw):
        return _Resp(401)  # revoked VT_API_KEY: every file lookup 401s

    monkeypatch.setattr(vt.requests, "request", fake_request)

    def fake_run(cmd, *a, **kw):
        if "view" in cmd and "isDraft" in cmd:
            return _FakeCompleted(stdout="true\n")  # the pre-edit draft re-check
        if "view" in cmd:
            return _FakeCompleted(stdout="existing release notes\n")
        return _FakeCompleted()  # the `gh release edit` call

    monkeypatch.setattr(vt.subprocess, "run", fake_run)
    monkeypatch.setenv("VT_API_KEY", "revoked-key")
    monkeypatch.setattr(vt.sys, "argv", ["vt_release_scan.py", "v9.9.9", str(bindir)])
    monkeypatch.chdir(tmp_path)  # main() writes _vt_body.md into cwd

    rc = vt.main()

    assert rc != 0, "a scan that could not be established must fail the release step (nonzero exit)"
    written = (tmp_path / "_vt_body.md").read_text(encoding="utf-8")
    assert "scan FAILED" in written, "the failed file must be labeled FAILED in the notes"
    assert "_scan pending_" not in written, "a real auth failure must not masquerade as 'pending'"


def test_main_aborts_notes_edit_if_no_longer_draft(monkeypatch, tmp_path):
    """If the release was published during the (long) scan, main() must refuse to edit its notes —
    the pre-edit draft re-check raises rather than mutating a public release."""
    bindir = tmp_path / "dist"
    bindir.mkdir()
    (bindir / "cyber-controller-linux-x64").write_bytes(b"BINARY-CONTENT")

    def fake_request(method, url, headers=None, timeout=None, **kw):
        return _Resp(200, {"data": {"attributes": {"last_analysis_stats":
            {"malicious": 0, "suspicious": 0, "undetected": 70, "harmless": 5}}}})

    monkeypatch.setattr(vt.requests, "request", fake_request)

    edited = {"n": 0}

    def fake_run(cmd, *a, **kw):
        if "view" in cmd and "isDraft" in cmd:
            return _FakeCompleted(stdout="false\n")  # published mid-scan
        if "edit" in cmd:
            edited["n"] += 1
        return _FakeCompleted(stdout="notes\n")

    monkeypatch.setattr(vt.subprocess, "run", fake_run)
    monkeypatch.setenv("VT_API_KEY", "k")
    monkeypatch.setattr(vt.sys, "argv", ["vt_release_scan.py", "v9.9.9", str(bindir)])
    monkeypatch.chdir(tmp_path)

    with pytest.raises(vt.VTError):
        vt.main()
    assert edited["n"] == 0, "must not edit the notes of a release that is no longer a draft"


# ── tally: what counts as complete scan evidence (a completed scan requirement) ────────────────

def _complete(malicious=0, suspicious=0, undetected=60, harmless=5):
    return {"malicious": malicious, "suspicious": suspicious,
            "undetected": undetected, "harmless": harmless,
            "timeout": 0, "type-unsupported": 3, "failure": 0}


def test_tally_counts_detections_and_engines():
    assert vt.tally(_complete(malicious=2, suspicious=1)) == (3, 68)


def test_tally_clean_scan():
    assert vt.tally(_complete()) == (0, 65)


def test_tally_pending_is_incomplete():
    with pytest.raises(vt.VTError):
        vt.tally(None)


def test_tally_empty_stats_is_incomplete():
    with pytest.raises(vt.VTError):
        vt.tally({})


def test_tally_non_dict_is_malformed():
    for bad in ("nope", 5, [1, 2]):
        with pytest.raises(vt.VTError):
            vt.tally(bad)


def test_tally_zero_evaluated_engines_is_incomplete():
    """A dict full of zeros (an analysis with no engine results) must fail, not read as 0/0."""
    with pytest.raises(vt.VTError):
        vt.tally({"malicious": 0, "suspicious": 0, "undetected": 0, "harmless": 0})


def test_tally_only_nonengine_buckets_is_incomplete():
    """timeout/type-unsupported/failure alone are not evaluations — no engine actually scanned."""
    with pytest.raises(vt.VTError):
        vt.tally({"malicious": 0, "suspicious": 0, "undetected": 0, "harmless": 0,
                  "timeout": 4, "type-unsupported": 2, "failure": 1})


def test_tally_non_integer_stat_is_malformed():
    with pytest.raises(vt.VTError):
        vt.tally({"malicious": "2", "undetected": 60, "harmless": 0, "suspicious": 0})
    with pytest.raises(vt.VTError):  # bool must not pass as an int count
        vt.tally({"malicious": True, "undetected": 60, "harmless": 0, "suspicious": 0})


# ── build_rows: aggregate pass/fail + review flagging (no network, get_stats injected) ─────────

def _bins(tmp_path, n=2):
    paths = []
    for i in range(n):
        p = tmp_path / f"cyber-controller-v2.0.1-plat{i}"
        p.write_bytes(b"binary-%d" % i)
        paths.append(str(p))
    return paths


def test_build_rows_all_clean_passes(tmp_path):
    files = _bins(tmp_path)
    rows, failed, flagged = vt.build_rows(
        files, lambda f: ("deadbeef", _complete()), sleep=lambda *_: None)
    assert failed is False and flagged == []
    assert len(rows) == 2 and all("0/65" in r for r in rows)


def test_build_rows_empty_file_list_fails():
    rows, failed, flagged = vt.build_rows([], lambda f: ("x", _complete()), sleep=lambda *_: None)
    assert failed is True and rows == []


def test_build_rows_pending_fails_with_honest_row(tmp_path):
    files = _bins(tmp_path, 1)
    rows, failed, flagged = vt.build_rows(files, lambda f: ("sha", None), sleep=lambda *_: None)
    assert failed is True and "scan FAILED" in rows[0]


def test_build_rows_vterror_fails_with_honest_row(tmp_path):
    files = _bins(tmp_path, 1)

    def boom(f):
        raise vt.VTError("bad key")

    rows, failed, flagged = vt.build_rows(files, boom, sleep=lambda *_: None)
    assert failed is True and "scan FAILED" in rows[0]


def test_build_rows_detections_are_flagged_not_dismissed(tmp_path):
    files = _bins(tmp_path, 1)
    rows, failed, flagged = vt.build_rows(
        files, lambda f: ("sha", _complete(malicious=3)), sleep=lambda *_: None)
    # A clean, completed scan WITH detections is not a failure, but it is flagged for review.
    assert failed is False
    assert flagged == ["cyber-controller-v2.0.1-plat0"]
    assert "3/" in rows[0]


def test_build_rows_mixed_batch_fails_on_any_incomplete(tmp_path):
    files = _bins(tmp_path, 2)
    seq = iter([("a", _complete()), ("b", None)])
    rows, failed, flagged = vt.build_rows(files, lambda f: next(seq), sleep=lambda *_: None)
    assert failed is True  # one complete + one pending -> overall fail
    assert len(rows) == 2
