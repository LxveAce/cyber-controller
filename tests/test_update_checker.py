"""Unit tests for the owned update-availability checker (src/core/update_checker.py).

Everything is deterministic: the network fetch and both clocks are injected, and the scheduler is driven by
gating the fake fetch + poking the wake event — no real network, no real sleeps beyond short poll loops.
"""
from __future__ import annotations

import socket
import threading
import time
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from src.core import update_checker as uc


def _release(tag, *, draft=False, prerelease=False, url=None):
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease,
            "html_url": url or f"https://github.com/LxveAce/cyber-controller/releases/tag/{tag}"}


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class _FakeFetch:
    """Injected fetch. Gate open by default (returns immediately); clear it to block a fetch in flight."""

    def __init__(self, result=None, error=None):
        self.calls = 0
        self.result = result if result is not None else []
        self.error = error
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()

    def __call__(self):
        self.calls += 1
        self.entered.set()
        if not self.gate.wait(3.0):
            raise AssertionError("fetch gate never released")
        if self.error is not None:
            raise self.error
        return self.result


def _wait_state(c, want, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if c.snapshot().state == want:
            return c.snapshot()
        time.sleep(0.005)
    raise AssertionError(f"state {want!r} not reached; got {c.snapshot().state!r}")


def _wait(cond, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return False


# ── construction / snapshot ──────────────────────────────────────────────────────────────────────

def test_initial_state_is_idle_no_thread_no_network():
    fetch = _FakeFetch()
    c = uc.UpdateChecker("2.0.1", fetch=fetch)
    snap = c.snapshot()
    assert snap.state == uc.IDLE and snap.revision == 0
    assert snap.enabled is True and snap.current == "2.0.1"
    assert snap.latest_tag is None and snap.latest_url is None and snap.checked_at is None
    assert snap.error_code is None and snap.retry_after_seconds == 0
    assert fetch.calls == 0                       # construction performs no network


def test_to_dict_is_schema_version_1_with_explicit_nulls():
    c = uc.UpdateChecker("2.0.1", fetch=_FakeFetch())
    d = c.snapshot().to_dict()
    assert d["schema_version"] == 1
    for key in ("runtime_id", "revision", "enabled", "state", "current",
                "latest_tag", "latest_url", "checked_at", "retry_after_seconds", "error_code"):
        assert key in d
    assert d["latest_tag"] is None and d["checked_at"] is None and d["error_code"] is None


# ── auto-check + classification ────────────────────────────────────────────────────────────────────

def test_start_runs_auto_check_up_to_date():
    fetch = _FakeFetch(result=[_release("v2.0.1")])
    c = uc.UpdateChecker("2.0.1", fetch=fetch)
    try:
        assert c.start() is True
        snap = _wait_state(c, uc.UP_TO_DATE)
        assert snap.checked_at is not None and snap.revision >= 1
        assert fetch.calls == 1
    finally:
        c.stop()


def test_available_when_a_published_release_is_ahead():
    fetch = _FakeFetch(result=[_release("v9.9.9")])
    c = uc.UpdateChecker("2.0.1", fetch=fetch)
    try:
        c.start()
        snap = _wait_state(c, uc.AVAILABLE)
        assert snap.latest_tag == "v9.9.9"
        assert snap.latest_url.endswith("/v9.9.9")
    finally:
        c.stop()


def test_prerelease_and_draft_are_not_offered():
    fetch = _FakeFetch(result=[_release("v9.9.9", prerelease=True), _release("v9.9.8", draft=True)])
    c = uc.UpdateChecker("2.0.1", fetch=fetch)
    try:
        c.start()
        snap = _wait_state(c, uc.UP_TO_DATE)     # neither counts as an available stable update
        assert snap.latest_tag is None
    finally:
        c.stop()


def test_offline_sets_state_and_finite_error_code():
    fetch = _FakeFetch(error=uc._FetchError(uc.ERR_OFFLINE))
    c = uc.UpdateChecker("2.0.1", fetch=fetch)
    try:
        c.start()
        snap = _wait_state(c, uc.OFFLINE)
        assert snap.error_code == uc.ERR_OFFLINE and snap.checked_at is not None
    finally:
        c.stop()


# ── enabled / disabled / manual ────────────────────────────────────────────────────────────────────

def test_disabled_does_not_auto_check_but_manual_works():
    fetch = _FakeFetch(result=[_release("v2.0.1")])
    c = uc.UpdateChecker("2.0.1", enabled=False, fetch=fetch)
    try:
        c.start()
        time.sleep(0.05)
        assert fetch.calls == 0 and c.snapshot().state == uc.IDLE   # parked while disabled
        out = c.request_check()
        assert out.accepted and out.reason == "started"
        _wait_state(c, uc.UP_TO_DATE)                               # manual works while disabled
        assert fetch.calls == 1
    finally:
        c.stop()


def test_disable_stops_future_auto_checks():
    clock = _Clock()
    fetch = _FakeFetch(result=[_release("v2.0.1")])
    c = uc.UpdateChecker("2.0.1", interval_seconds=100, fetch=fetch, monotonic=clock)
    try:
        c.start()
        _wait_state(c, uc.UP_TO_DATE)
        assert fetch.calls == 1
        c.set_enabled(False)
        clock.advance(1000)                 # well past the interval
        c._wake.set()                       # force the scheduler to re-evaluate
        time.sleep(0.05)
        assert fetch.calls == 1             # disabled -> no further automatic check
    finally:
        c.stop()


def test_reset_to_defaults_reenables_and_rechecks():
    fetch = _FakeFetch(result=[_release("v2.0.1")])
    c = uc.UpdateChecker("2.0.1", enabled=False, fetch=fetch)
    try:
        c.start()
        time.sleep(0.05)
        assert fetch.calls == 0
        c.reset_to_defaults()
        _wait_state(c, uc.UP_TO_DATE)
        assert c.snapshot().enabled is True and fetch.calls == 1
    finally:
        c.stop()


# ── coalescing + cooldown ──────────────────────────────────────────────────────────────────────────

def test_manual_callers_coalesce_into_one_in_flight_check():
    fetch = _FakeFetch(result=[_release("v2.0.1")])
    fetch.gate.clear()                      # block the in-flight check
    c = uc.UpdateChecker("2.0.1", enabled=False, fetch=fetch)
    try:
        c.start()
        assert c.request_check().reason == "started"
        assert fetch.entered.wait(2.0)      # the single check is now in flight
        for _ in range(5):
            assert c.request_check().reason == "coalesced"
        fetch.gate.set()
        _wait_state(c, uc.UP_TO_DATE)
        assert fetch.calls == 1             # exactly one fetch despite six requests
    finally:
        fetch.gate.set()
        c.stop()


def test_cooldown_denies_immediate_repeat_then_allows_after_clock_moves():
    clock = _Clock()
    fetch = _FakeFetch(result=[_release("v2.0.1")])
    c = uc.UpdateChecker("2.0.1", enabled=False, manual_cooldown_seconds=60, fetch=fetch, monotonic=clock)
    try:
        c.start()
        c.request_check()
        _wait_state(c, uc.UP_TO_DATE)
        assert fetch.calls == 1
        denied = c.request_check()
        assert denied.accepted is False and denied.reason == "cooldown"
        assert 0 < denied.retry_after_seconds <= 60
        clock.advance(61)                   # move the monotonic clock past the cooldown
        assert c.request_check().reason == "started"
        assert _wait(lambda: fetch.calls == 2)
    finally:
        c.stop()


# ── lifecycle: idempotent start, close, late result ─────────────────────────────────────────────────

def test_start_is_idempotent_and_noop_after_close():
    c = uc.UpdateChecker("2.0.1", enabled=False, fetch=_FakeFetch())
    assert c.start() is True
    assert c.start() is False               # a second start does not create a second owner
    assert c.stop() is True
    assert c.start() is False               # start after close is a no-op


def test_close_during_fetch_drops_the_result():
    fetch = _FakeFetch(result=[_release("v9.9.9")])
    fetch.gate.clear()                      # hold the fetch in flight
    c = uc.UpdateChecker("2.0.1", fetch=fetch)
    c.start()
    assert fetch.entered.wait(2.0)          # the auto-check is in flight
    completed = c.stop(timeout=0.2)         # scheduler is blocked in fetch -> incomplete shutdown
    assert completed is False
    fetch.gate.set()                        # let the (now retired) fetch finish
    time.sleep(0.1)
    snap = c.snapshot()
    assert snap.state != uc.AVAILABLE       # a retired runtime never publishes the late result
    assert snap.latest_tag is None


def test_constructor_failure_allows_clean_retry(monkeypatch):
    # A CONSTRUCTION-time failure (before _started is set) is definitely unstarted -> a retry builds a fresh
    # thread. (A Thread.start() failure is handled separately by test_start_error_retains_ownership_no_retry.)
    c = uc.UpdateChecker("2.0.1", enabled=False, fetch=_FakeFetch())
    real_thread = uc.threading.Thread

    def boom(*a, **k):
        raise RuntimeError("thread construction failed")

    monkeypatch.setattr(uc.threading, "Thread", boom)
    with pytest.raises(RuntimeError):
        c.start()
    monkeypatch.setattr(uc.threading, "Thread", real_thread)
    assert c.start() is True                # construction failure did not latch ownership
    c.stop()


def test_start_error_retains_ownership_no_retry(monkeypatch):
    # UC-1a: a Thread.start() error can coincide with a native thread already bootstrapping (is_alive() reads
    # False in that window), so ownership is RETAINED and a retry must NOT start a second scheduler.
    c = uc.UpdateChecker("2.0.1", enabled=False, fetch=_FakeFetch())

    def boom(self):
        raise RuntimeError("start reported an error")   # does not launch (simulates the ambiguous window)

    monkeypatch.setattr(uc.threading.Thread, "start", boom)
    with pytest.raises(RuntimeError):
        c.start()
    monkeypatch.undo()
    assert c._thread is not None                        # ownership retained (not cleared on is_alive()==False)
    assert c.start() is False                           # no second scheduler
    # UC-1a: a reported Thread.start() error cannot prove the native launch had no effect, so stop() honestly
    # reports incomplete (False) while that launch is unresolved — it never falsely claims completion.
    assert c.stop() is False


# ── revision only increments on a visible change ─────────────────────────────────────────────────────

def test_revision_stable_on_noop_enable():
    c = uc.UpdateChecker("2.0.1", enabled=True, fetch=_FakeFetch())
    r0 = c.snapshot().revision
    c.set_enabled(True)                     # already enabled -> no visible change
    assert c.snapshot().revision == r0
    c.set_enabled(False)                    # a real change
    assert c.snapshot().revision == r0 + 1


# ── persisted-timestamp hygiene ──────────────────────────────────────────────────────────────────────

def test_recent_persisted_timestamp_suppresses_the_launch_check():
    now = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(hours=1)).replace(microsecond=0).isoformat()
    fetch = _FakeFetch(result=[_release("v2.0.1")])
    c = uc.UpdateChecker("2.0.1", interval_seconds=24 * 3600, last_checked_at=recent,
                         fetch=fetch, wall_clock=lambda: now)
    try:
        assert c.snapshot().checked_at == recent
        c.start()
        time.sleep(0.05)
        assert fetch.calls == 0             # a recent check suppresses an immediate re-check
    finally:
        c.stop()


def test_future_persisted_timestamp_is_not_trusted():
    now = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)
    future = (now + timedelta(days=5)).replace(microsecond=0).isoformat()
    fetch = _FakeFetch(result=[_release("v2.0.1")])
    c = uc.UpdateChecker("2.0.1", last_checked_at=future, fetch=fetch, wall_clock=lambda: now)
    try:
        assert c.snapshot().checked_at is None    # a future stamp is discarded, not trusted
        c.start()
        _wait_state(c, uc.UP_TO_DATE)             # ...and it does not disable checks
        assert fetch.calls == 1
    finally:
        c.stop()


# ── bounded metadata reader ──────────────────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, body: bytes, chunk=None):
        self._body = body
        self._pos = 0
        self._chunk = chunk

    def read(self, n):
        step = min(self._chunk or n, n, len(self._body) - self._pos)
        out = self._body[self._pos:self._pos + step]
        self._pos += step
        return out

    read1 = read      # the reader prefers read1; expose the same bounded behavior

    def settimeout(self, t):   # the reader applies the remaining budget to the socket before each read
        pass

    def close(self):
        pass


class _FakeOpener:
    def __init__(self, resp):
        self._resp = resp

    def open(self, req, timeout=None):
        return self._resp


class _EofAdvancesClockResp:
    """Serves *chunks*, then on EOF advances the injected clock past the deadline (UC-2 post-deadline EOF)."""

    def __init__(self, chunks, clock, advance):
        self._chunks = list(chunks)
        self._clock = clock
        self._advance = advance

    def read1(self, n):
        if self._chunks:
            return self._chunks.pop(0)
        self._clock.advance(self._advance)
        return b""

    read = read1

    def settimeout(self, t):
        pass

    def close(self):
        pass


class _RaisingResp:
    def __init__(self, exc):
        self._exc = exc

    def read1(self, n):
        raise self._exc

    read = read1

    def settimeout(self, t):
        pass

    def close(self):
        pass


class _BudgetSock:
    def __init__(self):
        self.timeout = None
        self.applied = []

    def settimeout(self, t):
        self.timeout = t
        self.applied.append(t)


class _BudgetResp:
    """A response whose read1 blocks for each segment's duration, bounded by the CURRENT socket timeout: if a
    segment would block past the applied timeout, it advances the clock by the timeout and raises socket
    timeout. Proves the reader applies the REMAINING budget to each read (UC-2)."""

    def __init__(self, segments, clock):
        self._segments = list(segments)   # (bytes, block_seconds)
        self._clock = clock
        self._sock = _BudgetSock()

        class _Raw:
            def __init__(self, sock):
                self._sock = sock

        class _Fp:
            def __init__(self, raw):
                self.raw = raw

        self.fp = _Fp(_Raw(self._sock))

    def read1(self, n):
        if not self._segments:
            return b""
        data, block = self._segments.pop(0)
        t = self._sock.timeout
        if t is not None and block > t:
            self._clock.advance(t)
            raise socket.timeout("timed out")
        self._clock.advance(block)
        return data

    read = read1

    def close(self):
        pass


class _ClosedAfterBodySock:
    """A socket that raises on settimeout once closed — mirrors a real socket after the response closes it."""

    def __init__(self):
        self.closed = False

    def settimeout(self, t):
        if self.closed:
            raise OSError(10038, "an operation was attempted on something that is not a socket")


class _FramedResp:
    """A response over content-length/chunked framing: serves *segments*, and once the body is complete it
    CLOSES the fp+socket (isclosed()->True, socket.settimeout raises) — mirroring http.client.HTTPResponse,
    which closes the connection the instant a content-length/chunked body is fully read."""

    def __init__(self, segments):
        self._segs = list(segments)
        self._sock = _ClosedAfterBodySock()

        class _Raw:
            def __init__(s, sock):
                s._sock = sock

        class _Fp:
            def __init__(s, raw):
                s.raw = raw

        self.fp = _Fp(_Raw(self._sock))

    def _close_body(self):
        self._sock.closed = True
        self.fp = None

    def read1(self, n):
        if not self._segs:
            self._close_body()
            return b""
        seg = self._segs.pop(0)
        if not self._segs:          # last segment -> body complete -> connection closes
            self._close_body()
        return seg

    read = read1

    def isclosed(self):
        return self.fp is None

    def close(self):
        pass


# ── UC-1: launch/shutdown ownership ────────────────────────────────────────────────────────────────

def test_constructor_thread_failure_allows_retry(monkeypatch):
    c = uc.UpdateChecker("2.0.1", enabled=False, fetch=_FakeFetch())
    real_thread = uc.threading.Thread

    def boom(*a, **k):
        raise RuntimeError("thread construction failed")

    monkeypatch.setattr(uc.threading, "Thread", boom)
    with pytest.raises(RuntimeError):
        c.start()
    monkeypatch.setattr(uc.threading, "Thread", real_thread)
    assert c.start() is True          # a constructor failure did not permanently latch _started
    c.stop()


def test_start_then_raise_reraises_original_but_keeps_ownership(monkeypatch):
    # UC-1: a launch error after the worker entered must RE-RAISE the original error (not be swallowed as a
    # success), while the entered worker keeps ownership and stop() settles it.
    fetch = _FakeFetch(result=[_release("v2.0.1")])
    c = uc.UpdateChecker("2.0.1", enabled=False, fetch=fetch)
    real_start = uc.threading.Thread.start

    def start_then_raise(self):
        real_start(self)
        c._entered.wait(2.0)          # the worker has entered _run
        raise RuntimeError("start reported an error after the thread began")

    monkeypatch.setattr(uc.threading.Thread, "start", start_then_raise)
    with pytest.raises(RuntimeError):
        c.start()
    monkeypatch.undo()
    assert c._thread is not None       # ownership retained despite the re-raise
    assert c.stop() is True            # ...and the owner settles on close


def test_start_raise_before_entry_retains_ownership_and_reports_incomplete(monkeypatch):
    # UC-1: a native thread that STARTED but has not yet reached _run (so _entered is clear) must not have its
    # ownership cleared — _entered==False is not proof it never started. stop() reports incomplete while it is
    # still alive, and completes once it is released.
    gate = threading.Event()
    started_os = threading.Event()
    c = uc.UpdateChecker("2.0.1", enabled=False, fetch=_FakeFetch())
    orig_run = c._run

    def gated_run():
        started_os.set()
        gate.wait(2.0)                # block BEFORE entering the real _run (so _entered stays clear a while)
        orig_run()

    monkeypatch.setattr(c, "_run", gated_run)
    real_start = uc.threading.Thread.start

    def start_then_control(self):
        real_start(self)
        started_os.wait(2.0)          # the OS thread is running but has not entered _run
        raise KeyboardInterrupt()

    monkeypatch.setattr(uc.threading.Thread, "start", start_then_control)
    with pytest.raises(KeyboardInterrupt):
        c.start()
    monkeypatch.undo()
    assert c._thread is not None                 # ownership retained (thread alive, not entered)
    assert c.stop(timeout=0.1) is False          # still alive -> honest incomplete, no false 'complete'
    gate.set()                                    # release; it enters _run, sees closed, exits
    assert c.stop() is True


def test_stop_does_not_join_an_unstarted_thread():
    c = uc.UpdateChecker("2.0.1", enabled=False, fetch=_FakeFetch())
    # Simulate start() in flight: a thread recorded but not yet launched (its OS thread never started).
    c._thread = uc.threading.Thread(target=lambda: None, daemon=True)
    c._started = True
    c._thread_launched = False
    result = c.stop(timeout=0.1)       # must NOT call join() on an unstarted thread (that raises)
    assert result is False             # honest incomplete, no RuntimeError


def test_stop_reports_incomplete_for_launched_but_unstarted_thread():
    # UC-1a[stop] discriminator: a LAUNCHED thread whose OS bootstrap has not yet set Python's _started event
    # makes join() raise RuntimeError. That exception cannot prove the native worker is dead, so stop() must
    # report honest incomplete (False) and never claim completion. An unstarted Thread object reproduces the
    # join-before-start RuntimeError deterministically. The pre-correction code returned True on exactly this
    # path (the bug); this asserts the corrected honest-incomplete outcome.
    c = uc.UpdateChecker("2.0.1", enabled=False, fetch=_FakeFetch())
    c._thread = uc.threading.Thread(target=lambda: None, daemon=True)  # never .start()ed -> join() raises
    c._started = True
    c._thread_launched = True           # start() recorded a launch; the native thread may be bootstrapping
    assert c.stop(timeout=0.0) is False


def test_stop_while_fetch_blocked_reports_incomplete():
    fetch = _FakeFetch(result=[_release("v2.0.1")])
    fetch.gate.clear()
    c = uc.UpdateChecker("2.0.1", fetch=fetch)
    c.start()
    assert fetch.entered.wait(2.0)
    assert c.stop(timeout=0.0) is False   # a blocked scheduler is not confirmed stopped
    fetch.gate.set()
    c.stop()


# ── UC-2: transfer deadline completeness + timeout code ─────────────────────────────────────────────

def test_post_deadline_eof_is_a_timeout(monkeypatch):
    clock = _Clock(0.0)
    resp = _EofAdvancesClockResp([b"[]"], clock, advance=100.0)   # EOF arrives after the 15s deadline
    monkeypatch.setattr(uc.flash_core, "_OPENER", _FakeOpener(resp))
    with pytest.raises(uc._FetchError) as ei:
        uc.fetch_releases_bounded(15.0, monotonic=clock)
    assert ei.value.error_code == uc.ERR_TIMEOUT


def test_transport_timeout_maps_to_timeout_code(monkeypatch):
    monkeypatch.setattr(uc.flash_core, "_OPENER", _FakeOpener(_RaisingResp(TimeoutError("read timed out"))))
    with pytest.raises(uc._FetchError) as ei:
        uc.fetch_releases_bounded(10.0)
    assert ei.value.error_code == uc.ERR_TIMEOUT


# ── UC-3: over-cap item count is rejected, not truncated ────────────────────────────────────────────

def test_over_cap_item_count_is_rejected(monkeypatch):
    monkeypatch.setattr(uc, "MAX_RELEASE_ITEMS", 3)
    body = b'[{"tag_name":"a"},{"tag_name":"b"},{"tag_name":"c"},{"tag_name":"v9.9.9"}]'   # 4 > 3
    monkeypatch.setattr(uc.flash_core, "_OPENER", _FakeOpener(_FakeResp(body)))
    with pytest.raises(uc._FetchError) as ei:
        uc.fetch_releases_bounded(10.0)
    assert ei.value.error_code == uc.ERR_TOO_LARGE


# ── UC-4: nonfinite / out-of-range scheduling inputs are rejected at construction ───────────────────

@pytest.mark.parametrize("kwargs", [
    {"interval_seconds": 0},
    {"interval_seconds": -5},
    {"interval_seconds": float("nan")},
    {"interval_seconds": float("inf")},
    {"manual_cooldown_seconds": float("inf")},
    {"manual_cooldown_seconds": float("nan")},
    {"fetch_deadline_seconds": float("nan")},
    {"fetch_deadline_seconds": 0},
])
def test_constructor_rejects_bad_scheduling_inputs(kwargs):
    with pytest.raises(ValueError):
        uc.UpdateChecker("2.0.1", fetch=_FakeFetch(), **kwargs)


def test_constructor_accepts_zero_cooldown_for_tests():
    c = uc.UpdateChecker("2.0.1", manual_cooldown_seconds=0, fetch=_FakeFetch())
    assert c.snapshot().state == uc.IDLE


# ── UC-5: snapshot release URL is validated, not passed through ─────────────────────────────────────

@pytest.mark.parametrize("bad_url", [
    "javascript:alert(1)",
    "https://evil.example.com/LxveAce/cyber-controller/releases/tag/v9.9.9",
    "https://github.com/someone-else/other-repo/releases/tag/v9.9.9",
    "http://github.com/LxveAce/cyber-controller/releases/tag/v9.9.9",   # not https
])
def test_classify_rejects_untrusted_release_url(bad_url):
    state, tag, url = uc.classify_releases("2.0.1", [_release("v9.9.9", url=bad_url)])
    assert state == uc.AVAILABLE and tag == "v9.9.9"
    assert url == uc.updater.RELEASES_PAGE          # falls back to the known page, never the untrusted URL


def test_classify_accepts_valid_repo_release_url():
    good = "https://github.com/LxveAce/cyber-controller/releases/tag/v9.9.9"
    state, tag, url = uc.classify_releases("2.0.1", [_release("v9.9.9", url=good)])
    assert state == uc.AVAILABLE and url == good


@pytest.mark.parametrize("bad_url", [
    "https://github.com/LxveAce/cyber-controller/issues/1",                              # not a release page
    "https://github.com/LxveAce/cyber-controller/../../other/project/releases/tag/v9",   # dot-segment escape
    "https://github.com:8443/LxveAce/cyber-controller/releases/tag/v9.9.9",              # unexpected port
    "https://user:pass@github.com/LxveAce/cyber-controller/releases/tag/v9.9.9",         # userinfo
])
def test_classify_rejects_non_release_or_spoofed_authority_urls(bad_url):
    state, tag, url = uc.classify_releases("2.0.1", [_release("v9.9.9", url=bad_url)])
    assert state == uc.AVAILABLE
    assert url == uc.updater.RELEASES_PAGE     # never the issues page / escaped / spoofed-authority link


# ── UC-2: the remaining budget is applied to each body read ─────────────────────────────────────────

def test_body_read_budget_is_applied_per_read(monkeypatch):
    clock = _Clock(0.0)
    # First segment blocks 14s (within the 15s budget); the second would block 10s, but only ~1s of budget
    # remains, so the read must be bounded to that and time out — total elapsed ~15s, not 24s.
    resp = _BudgetResp([(b"[", 14.0), (b"]", 10.0)], clock)
    monkeypatch.setattr(uc.flash_core, "_OPENER", _FakeOpener(resp))
    with pytest.raises(uc._FetchError) as ei:
        uc.fetch_releases_bounded(15.0, monotonic=clock)
    assert ei.value.error_code == uc.ERR_TIMEOUT
    assert clock.t <= 16.0                      # the per-read budget was honored (not blocked out to 24s)
    assert resp._sock.applied[-1] <= 2.0        # the last read's socket timeout was the remaining budget


def test_urlerror_wrapping_timeout_maps_to_timeout_code(monkeypatch):
    class _TimingOutOpener:
        def open(self, req, timeout=None):
            raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(uc.flash_core, "_OPENER", _TimingOutOpener())
    with pytest.raises(uc._FetchError) as ei:
        uc.fetch_releases_bounded(10.0)
    assert ei.value.error_code == uc.ERR_TIMEOUT     # not misclassified as offline


def test_content_length_body_that_closes_the_socket_still_parses(monkeypatch):
    # A valid Content-Length: 2 body "[]": the final read1 closes the fp+socket. The reader must not
    # settimeout the now-closed socket (which raises 10038 and used to be misreported as offline).
    monkeypatch.setattr(uc.flash_core, "_OPENER", _FakeOpener(_FramedResp([b"[]"])))
    assert uc.fetch_releases_bounded(10.0) == []


def test_chunked_multi_segment_body_parses(monkeypatch):
    monkeypatch.setattr(uc.flash_core, "_OPENER",
                        _FakeOpener(_FramedResp([b'[{"tag_name":', b'"v2.0.1"}]'])))
    out = uc.fetch_releases_bounded(10.0)
    assert out == [{"tag_name": "v2.0.1"}]


def test_empty_body_that_closes_is_a_payload_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(uc.flash_core, "_OPENER", _FakeOpener(_FramedResp([])))
    with pytest.raises(uc._FetchError) as ei:
        uc.fetch_releases_bounded(10.0)
    assert ei.value.error_code == uc.ERR_PAYLOAD      # empty body isn't a list; no settimeout-on-closed crash


# ── UC-6: persisted display timestamps are normalized to UTC / naive rejected ───────────────────────

def test_persisted_offset_timestamp_is_normalized_to_utc():
    now = datetime(2026, 9, 6, 20, 0, 0, tzinfo=timezone.utc)
    offset = "2026-09-06T12:00:00-07:00"                 # same instant as 19:00:00Z
    c = uc.UpdateChecker("2.0.1", last_checked_at=offset, fetch=_FakeFetch(), wall_clock=lambda: now)
    assert c.snapshot().checked_at == "2026-09-06T19:00:00+00:00"


def test_persisted_naive_timestamp_is_rejected():
    now = datetime(2026, 9, 6, 20, 0, 0, tzinfo=timezone.utc)
    naive = "2026-09-06T12:00:00"                        # no offset
    c = uc.UpdateChecker("2.0.1", last_checked_at=naive, fetch=_FakeFetch(), wall_clock=lambda: now)
    assert c.snapshot().checked_at is None


def test_bounded_reader_parses_a_small_list(monkeypatch):
    body = b'[{"tag_name": "v2.0.1"}]'
    monkeypatch.setattr(uc.flash_core, "_OPENER", _FakeOpener(_FakeResp(body)))
    out = uc.fetch_releases_bounded(10.0)
    assert isinstance(out, list) and out[0]["tag_name"] == "v2.0.1"


def test_bounded_reader_rejects_oversized_body(monkeypatch):
    monkeypatch.setattr(uc, "MAX_JSON_BYTES", 64)
    body = b"[" + b" " * 200 + b"]"
    monkeypatch.setattr(uc.flash_core, "_OPENER", _FakeOpener(_FakeResp(body, chunk=32)))
    with pytest.raises(uc._FetchError) as ei:
        uc.fetch_releases_bounded(10.0)
    assert ei.value.error_code == uc.ERR_TOO_LARGE


def test_bounded_reader_enforces_deadline(monkeypatch):
    monkeypatch.setattr(uc.flash_core, "_OPENER", _FakeOpener(_FakeResp(b"[]")))
    ticks = iter([0.0, 100.0, 100.0])       # start at 0, then jump past the 15s deadline
    with pytest.raises(uc._FetchError) as ei:
        uc.fetch_releases_bounded(15.0, monotonic=lambda: next(ticks))
    assert ei.value.error_code == uc.ERR_TIMEOUT


def test_bounded_reader_rejects_non_list_payload(monkeypatch):
    monkeypatch.setattr(uc.flash_core, "_OPENER", _FakeOpener(_FakeResp(b'{"not": "a list"}')))
    with pytest.raises(uc._FetchError) as ei:
        uc.fetch_releases_bounded(10.0)
    assert ei.value.error_code == uc.ERR_PAYLOAD


def test_bounded_reader_drip_feed_completes(monkeypatch):
    body = b'[{"tag_name": "v2.0.1"}, {"tag_name": "v2.0.0"}]'
    monkeypatch.setattr(uc.flash_core, "_OPENER", _FakeOpener(_FakeResp(body, chunk=4)))   # 4 bytes/read
    out = uc.fetch_releases_bounded(10.0)
    assert [r["tag_name"] for r in out] == ["v2.0.1", "v2.0.0"]


# ── UC-1b: an unexpected worker exit retires the runtime ─────────────────────────────────────────────

def test_worker_control_exit_retires_the_runtime():
    captured: list = []
    fetch = _FakeFetch(error=KeyboardInterrupt())
    c = uc.UpdateChecker("2.0.1", fetch=fetch)
    old_hook = threading.excepthook
    threading.excepthook = lambda args: captured.append(args.exc_value)
    try:
        c.start()
        assert _wait(lambda: c.snapshot().state == uc.ERROR)   # not permanent 'checking'
        time.sleep(0.05)
    finally:
        threading.excepthook = old_hook
    snap = c.snapshot()
    assert snap.state == uc.ERROR and snap.error_code == uc.ERR_WORKER_EXIT
    assert c.request_check().reason == "closed"       # a dead owner rejects future requests, not 'coalesced'
    assert captured and isinstance(captured[0], KeyboardInterrupt)   # original control preserved


# ── UC-2a: an incomplete Content-Length body is a finite transport failure ───────────────────────────

class _IncompleteResp:
    """Declares more Content-Length than it serves; `length` stays > 0 after EOF (RFC 9112 §6.3)."""

    def __init__(self, body, declared):
        self._body = body
        self._served = False
        self.length = declared
        self._sock = _ClosedAfterBodySock()

        class _Raw:
            def __init__(s, sock):
                s._sock = sock

        class _Fp:
            def __init__(s, raw):
                s.raw = raw

        self.fp = _Fp(_Raw(self._sock))

    def read1(self, n):
        if self._served:
            return b""
        self._served = True
        self.length -= len(self._body)     # partial delivery leaves length > 0
        self._sock.closed = True
        self.fp = None
        return self._body

    read = read1

    def isclosed(self):
        return self.fp is None

    def close(self):
        pass


def test_incomplete_content_length_body_is_offline(monkeypatch):
    monkeypatch.setattr(uc.flash_core, "_OPENER", _FakeOpener(_IncompleteResp(b"[]", declared=3)))
    with pytest.raises(uc._FetchError) as ei:
        uc.fetch_releases_bounded(10.0)
    assert ei.value.error_code == uc.ERR_OFFLINE       # short body is not a parseable success


# ── UC-2b: the deadline socket re-applies the remaining budget to each underlying read ───────────────

def test_deadline_socket_rebounds_each_read():
    clock = _Clock(0.0)
    events = []

    class _Raw:
        def settimeout(self, t):
            events.append(("settimeout", round(t, 3)))

        def recv_into(self, buf):
            events.append(("recv", round(clock.t, 3)))
            return 0

    proxy = uc._DeadlineSocket(_Raw(), lambda: 15.0 - clock.t)
    proxy.recv_into(bytearray(4))
    clock.advance(14.0)
    proxy.recv_into(bytearray(4))
    timeouts = [t for (k, t) in events if k == "settimeout"]
    assert timeouts[0] == 15.0 and abs(timeouts[1] - 1.0) < 0.01   # budget re-applied per read, not once


def test_deadline_socket_raises_when_budget_exhausted():
    clock = _Clock(0.0)

    class _Raw:
        def settimeout(self, t):
            pass

        def recv_into(self, buf):
            return 0

    proxy = uc._DeadlineSocket(_Raw(), lambda: 15.0 - clock.t)
    clock.advance(20.0)
    with pytest.raises(socket.timeout):
        proxy.recv_into(bytearray(4))


def _real_response(raw_bytes):
    import http.client
    s1, s2 = socket.socketpair()
    s2.sendall(raw_bytes)
    s2.close()
    resp = http.client.HTTPResponse(s1, method="GET")
    resp.begin()
    return resp, s1


def test_real_httpresponse_content_length_parses(monkeypatch):
    body = b'[{"tag_name": "v2.0.1"}]'
    raw = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    resp, s1 = _real_response(raw)

    class _Op:
        def open(self, req, timeout=None):
            return resp

    monkeypatch.setattr(uc.flash_core, "_OPENER", _Op())
    try:
        assert uc.fetch_releases_bounded(10.0) == [{"tag_name": "v2.0.1"}]
    finally:
        s1.close()


def test_real_httpresponse_chunked_parses(monkeypatch):
    body = b'[{"tag_name": "v2.0.1"}]'
    chunked = b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body)
    raw = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + chunked
    resp, s1 = _real_response(raw)

    class _Op:
        def open(self, req, timeout=None):
            return resp

    monkeypatch.setattr(uc.flash_core, "_OPENER", _Op())
    try:
        assert uc.fetch_releases_bounded(10.0) == [{"tag_name": "v2.0.1"}]
    finally:
        s1.close()


# ── UC-5: browser-normalized escapes are rejected ────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "https://github.com/LxveAce/cyber-controller/releases/%2e%2e/%2e%2e/other/project/releases/tag/v9",
    "https://github.com/LxveAce/cyber-controller/releases/..\\..\\other/project/releases/tag/v9",
    "https://github.com/LxveAce/cyber-controller/releases/%2E%2E/other/releases/tag/v9",   # uppercase encode
])
def test_classify_rejects_encoded_or_backslash_escapes(bad):
    state, tag, url = uc.classify_releases("2.0.1", [_release("v9.9.9", url=bad)])
    assert state == uc.AVAILABLE
    assert url == uc.updater.RELEASES_PAGE     # falls back; never a link a browser normalizes out of the repo
