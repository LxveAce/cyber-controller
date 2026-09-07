"""Unit controls for the memory-only BLE-history runtime adapter (transport-agnostic).

Real in-memory BleJournal + accepted policy + real projected facts. No disk, settings loader,
device,
network, or Flask. Covers status per mode, paging + cursor-advance, expiry/future/malformed cursors,
detached rows, start-failure ownership, close resolution, and a no-ambient-filesystem proof.
"""
from __future__ import annotations

import builtins
import os

import pytest

from src.core.ble_fact_projection import project_addressed
from src.core.ble_history_policy import decide_ble_history_policy
from src.ui.web import ble_history_runtime as hr
from src.ui.web.ble_history_runtime import BleHistoryRuntime

_SRC = {"port": "COM1", "firmware": "fw", "connection_id": "1"}

_MEMORY = {"ble_history": {"mode": "memory"}}
_PERSISTENT = {"ble_history": {"mode": "persistent", "plaintext_ack": True},
               "security": {"secure_container": False}}


def _runtime(settings):
    return BleHistoryRuntime(decide_ble_history_policy(settings))


def _submit(rt, n):
    """Submit *n* valid addressed facts into the started adapter's journal; returns the sink."""
    sink = rt.sink
    for i in range(n):
        fact = project_addressed(
            {"mac": f"aa:bb:cc:dd:ee:{i:02x}", "name": f"n{i}", "rssi": -50, "type": "public"},
            _SRC)
        assert fact is not None
        assert sink.submit(fact) == "queued"
    return sink


def _ready(n=0):
    """A started memory adapter carrying *n* submitted rows."""
    rt = _runtime(_MEMORY)
    rt.start()
    if n:
        _submit(rt, n)
    return rt


def _seqs(result):
    return [row["seq"] for row in result.body["rows"]]


# ── status per mode / no journal for non-memory ─────────────────────────────

def test_disabled_creates_no_journal_and_reads_empty():
    rt = _runtime({})
    rt.start()
    assert rt.sink is None and rt._journal is None
    st = rt.status()
    assert st["effective_mode"] == "disabled" and st["available"] is False
    res = rt.read_page()
    assert res.outcome == hr.OUTCOME_DISABLED
    assert res.body["rows"] == [] and res.body["has_more"] is False


def test_persistent_is_unavailable_no_fallback_no_journal():
    rt = _runtime(_PERSISTENT)
    rt.start()
    assert rt.sink is None and rt._journal is None
    assert rt.status()["reason"] == "persistent_not_implemented"
    assert rt.read_page().outcome == hr.OUTCOME_UNAVAILABLE


def test_malformed_policy_is_unavailable():
    rt = _runtime({"ble_history": {"mode": 123}})
    rt.start()
    assert rt.sink is None
    assert rt.read_page().outcome == hr.OUTCOME_UNAVAILABLE


def test_memory_start_exposes_sink_and_available_status():
    rt = _ready()
    assert rt.sink is not None
    st = rt.status()
    assert st["effective_mode"] == "memory" and st["storage"] == "memory"
    assert st["available"] is True and st["durable"] is False
    assert st["policy_lifetime"] == "restart" and st["run_id"]
    assert st["counters"]["admitted"] == 0


# ── paging + cursor advance ──────────────────────────────────────────────────

def test_paging_advances_only_through_returned_rows_and_reports_has_more():
    rt = _ready(5)
    p1 = rt.read_page(limit="2")
    assert p1.outcome == hr.OUTCOME_OK and _seqs(p1) == [1, 2]
    assert p1.body["has_more"] is True and p1.body["cursor"].endswith(":2:0")
    p2 = rt.read_page(cursor=p1.body["cursor"], limit="2")
    assert _seqs(p2) == [3, 4] and p2.body["has_more"] is True
    p3 = rt.read_page(cursor=p2.body["cursor"], limit="2")
    assert _seqs(p3) == [5] and p3.body["has_more"] is False


def test_caught_up_resume_then_new_arrival_is_not_skipped():
    rt = _ready(2)
    sink = rt.sink
    p = rt.read_page()
    assert _seqs(p) == [1, 2] and p.body["has_more"] is False
    resume = p.body["cursor"]
    empty = rt.read_page(cursor=resume)
    assert empty.body["rows"] == [] and empty.body["cursor"] == resume
    late = project_addressed(
        {"mac": "aa:bb:cc:dd:ee:ff", "name": "late", "rssi": -40, "type": "public"}, _SRC)
    sink.submit(late)
    after = rt.read_page(cursor=resume)
    assert _seqs(after) == [3]


def test_fresh_empty_run_returns_seq_zero_cursor():
    rt = _ready()
    p = rt.read_page()
    assert p.outcome == hr.OUTCOME_OK and p.body["rows"] == []
    assert p.body["cursor"] == f"0:{rt._run_id}:0:0" and p.body["has_more"] is False


# ── cursor / limit validation (before touching the journal) ──────────────────

@pytest.mark.parametrize("bad", [
    "nope", "1:x:2:0", "0:abc:2:0", "0:0123456789abcdef:x:0",
    "0:0123456789abcdef:2:1", "0:0123456789abcdef:2", "x" * 130,
])
def test_malformed_cursor_is_bad_request(bad):
    rt = _ready(1)
    assert rt.read_page(cursor=bad).outcome == hr.OUTCOME_BAD_REQUEST


@pytest.mark.parametrize("bad", ["0", "257", "-1", "9x", "1000"])
def test_malformed_or_out_of_range_limit_is_bad_request(bad):
    rt = _ready()
    assert rt.read_page(limit=bad).outcome == hr.OUTCOME_BAD_REQUEST


def test_future_offset_is_bad_request():
    rt = _ready(2)   # confirmed_seq == 2
    assert rt.read_page(cursor=f"0:{rt._run_id}:9:0").outcome == hr.OUTCOME_BAD_REQUEST


def test_foreign_run_cursor_is_expired_not_rebased():
    rt = _ready(2)
    res = rt.read_page(cursor="0:ffffffffffffffff:1:0")
    assert res.outcome == hr.OUTCOME_EXPIRED and res.body["reason"] == "cursor_expired"


def test_detached_rows_cannot_mutate_retained_state():
    rt = _ready(1)
    rt.read_page().body["rows"][0]["label"] = "TAMPERED"
    assert rt.read_page().body["rows"][0].get("label") != "TAMPERED"


# ── start failure / close / no ambient fs ────────────────────────────────────

def test_ordinary_start_failure_is_owned_reported_no_sink(monkeypatch):
    class _Boom(hr.BleJournal):
        def start(self):
            raise RuntimeError("start failed")

    monkeypatch.setattr(hr, "BleJournal", _Boom)
    rt = _runtime(_MEMORY)
    rt.start()
    assert rt.sink is None and rt._journal is not None   # failed journal retained for cleanup
    assert rt.status()["reason"] == "start_failed"
    assert rt.read_page().outcome == hr.OUTCOME_UNAVAILABLE


def test_memory_close_resolves_and_releases():
    rt = _ready(3)
    result = rt.close()
    assert result is not None and result.resolved is True and result.lock_released is True


def test_no_ambient_filesystem_operations(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("forbidden filesystem operation")

    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(os, "makedirs", boom)
    monkeypatch.setattr(os, "mkdir", boom)
    rt = _ready(2)
    page = rt.read_page()
    assert _seqs(page) == [1, 2]
    assert rt.close().resolved is True


# ── C2: syntax validation never touches the journal ──────────────────────────

class _CountingJournal:
    """Wraps a real BleJournal and tallies each method-name access, to prove that a read path does
    (or does not) touch the journal. Delegation is transparent."""

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "calls", {})

    def __getattr__(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1
        return getattr(self._inner, name)


def test_malformed_and_foreign_queries_never_touch_the_journal():
    rt = _ready(2)
    spy = _CountingJournal(rt._journal)
    rt._journal = spy
    spy.calls.clear()
    for kwargs in (
        {"cursor": "nope"}, {"limit": "abc"}, {"limit": "0"}, {"limit": "257"},
        {"cursor": "0:ffffffffffffffff:1:0"},   # foreign run -> expired before any counters()
    ):
        res = rt.read_page(**kwargs)
        assert res.outcome in (hr.OUTCOME_BAD_REQUEST, hr.OUTCOME_EXPIRED)
    assert spy.calls == {}, spy.calls   # zero journal access for syntax/identity rejections
    ok = rt.read_page(limit="2")        # a valid page DOES read the journal
    assert ok.outcome == hr.OUTCOME_OK and spy.calls.get("read", 0) >= 1


def test_retained_start_failure_status_and_reject_never_touch_the_journal(monkeypatch):
    class _Boom(hr.BleJournal):
        def start(self):
            raise RuntimeError("start failed")

    monkeypatch.setattr(hr, "BleJournal", _Boom)
    rt = _runtime(_MEMORY)
    rt.start()   # start_failed; the failed journal is retained for cleanup
    assert rt._journal is not None and not rt._started
    spy = _CountingJournal(rt._journal)
    rt._journal = spy
    spy.calls.clear()
    assert rt.status()["reason"] == "start_failed"           # metadata only
    assert rt.read_page(limit="abc").outcome == hr.OUTCOME_UNAVAILABLE
    assert rt.read_page().outcome == hr.OUTCOME_UNAVAILABLE
    assert spy.calls == {}, spy.calls   # a retained start-failure state never calls the journal
