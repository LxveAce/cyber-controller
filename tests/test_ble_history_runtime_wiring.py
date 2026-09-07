"""Wiring controls for the memory-only BLE-history runtime slice: deferred-hub injection ordering,
the authenticated read route, and the dependent close-after-hub-drain stage.

Real journal + accepted policy + Flask test client; a few minimal fakes drive the close
failure/unresolved-retry paths. No disk, no device I/O, no network. Complements the transport-free
adapter units in test_ble_history_runtime.py.
"""
from __future__ import annotations

import pytest

pytest.importorskip("flask")

from src.core.ble_fact_projection import project_addressed
from src.core.ble_history_policy import decide_ble_history_policy
from src.core.cross_comm import EventBus, TargetPool
from src.core.cross_comm_hub import CrossCommHub
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.protocols.marauder import MarauderProtocol
from src.ui.web import ble_history_runtime as hr
from src.ui.web.app import create_app
from src.ui.web.ble_history_runtime import BleHistoryRuntime
from src.ui.web.update_runtime import WebRuntimeCleanup

_MEMORY = {"ble_history": {"mode": "memory"}}
_SRC = {"port": "COM1", "firmware": "fw", "connection_id": "1"}


def _started(settings=_MEMORY, rows=0):
    """A started BleHistoryRuntime for *settings*, pre-seeded with *rows* addressed facts."""
    rt = BleHistoryRuntime(decide_ble_history_policy(settings))
    rt.start()
    for i in range(rows):
        fact = project_addressed(
            {"mac": f"aa:bb:cc:dd:ee:{i:02x}", "name": f"n{i}", "rssi": -50, "type": "public"},
            _SRC)
        assert fact is not None and rt.sink.submit(fact) == "queued"
    return rt


class ScanInput:
    port = "SYNTHETIC_BLE"

    def on_line(self, callback):
        self.feed = callback


# ── deferred-hub injection ordering ──────────────────────────────────────────

def test_deferred_hub_is_inert_until_initialize_injects_started_sink():
    history = _started()
    hub = CrossCommHub(DeviceManager(), defer=True)
    try:
        assert hub.ingestor is None   # inert: no subscription can submit before the sink exists
        hub.initialize(journal=history.sink)
        assert hub.ingestor is not None
        assert hub.ingestor._journal is history.sink   # the SAME started journal, injected
        conn = ScanInput()
        hub.ingestor.attach(conn, MarauderProtocol())
        conn.feed("-62 Device: 02:00:00:00:00:01")
        page = history.read_page()
        assert page.outcome == hr.OUTCOME_OK
        assert len(page.body["rows"]) >= 1   # an admitted BLE fact reached the started sink
    finally:
        hub.close()
        history.close()


def test_initialize_is_one_shot_and_refuses_a_fenced_hub():
    history = _started()
    hub = CrossCommHub(DeviceManager(), defer=True)
    hub.initialize(journal=history.sink)
    with pytest.raises(RuntimeError):
        hub.initialize(journal=history.sink)   # one-shot
    hub.close()
    history.close()
    dead = CrossCommHub(DeviceManager(), defer=True)
    dead.close()
    with pytest.raises(RuntimeError):
        dead.initialize(journal=None)   # fenced/closed hub


def test_eager_construction_still_has_no_history_sink():
    hub = CrossCommHub(DeviceManager())   # default eager path, unchanged for legacy callers
    try:
        assert hub.ingestor is not None and hub.ingestor._journal is None
    finally:
        hub.close()


# ── authenticated read route ─────────────────────────────────────────────────

@pytest.fixture
def route_client(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")

    def build(history):
        app, _ = create_app(
            DeviceManager(), FlashEngine(), EventBus(), TargetPool(), ble_history=history)
        client = app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True
            session["cred_gen"] = app.extensions["cc_web_credentials"].generation
        return app, client

    return build


def test_route_requires_authentication(route_client):
    app, _ = route_client(_started(rows=1))
    assert app.test_client().get("/api/ble-history").status_code == 401


def test_route_returns_rows_no_store_and_honest_status(route_client):
    _, client = route_client(_started(rows=3))
    resp = client.get("/api/ble-history")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    body = resp.get_json()
    assert len(body["rows"]) == 3
    assert body["status"]["effective_mode"] == "memory"
    assert body["status"]["durable"] is False


def test_route_pages_through_returned_rows(route_client):
    _, client = route_client(_started(rows=3))
    p1 = client.get("/api/ble-history?limit=2").get_json()
    assert [r["seq"] for r in p1["rows"]] == [1, 2] and p1["has_more"] is True
    p2 = client.get(f"/api/ble-history?cursor={p1['cursor']}&limit=2").get_json()
    assert [r["seq"] for r in p2["rows"]] == [3] and p2["has_more"] is False


@pytest.mark.parametrize("query,status", [
    ("?limit=abc", 400), ("?limit=0", 400), ("?cursor=nope", 400),
    ("?cursor=0:ffffffffffffffff:1:0", 410), ("?limit=1&limit=2", 400), ("?foo=1", 400),
])
def test_route_bad_and_foreign_requests_map_to_status(route_client, query, status):
    _, client = route_client(_started(rows=2))
    assert client.get("/api/ble-history" + query).status_code == status


def test_route_duplicate_and_unknown_params_are_named(route_client):
    _, client = route_client(_started(rows=1))
    dup = client.get("/api/ble-history?limit=1&limit=2").get_json()
    unknown = client.get("/api/ble-history?foo=1").get_json()
    assert dup["error"] == "duplicate_query_param"
    assert unknown["error"] == "unknown_query_param"


def test_route_disabled_is_200_empty(route_client):
    _, client = route_client(_started(settings={}))   # no ble_history key -> disabled
    resp = client.get("/api/ble-history")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["rows"] == [] and body["status"]["effective_mode"] == "disabled"


def test_route_missing_injection_is_503(route_client):
    _, client = route_client(None)   # create_app built without a history adapter
    assert client.get("/api/ble-history").status_code == 503


def test_route_is_read_only_and_idempotent(route_client):
    _, client = route_client(_started(rows=2))
    first = client.get("/api/ble-history").get_json()["rows"]
    second = client.get("/api/ble-history").get_json()["rows"]
    assert [r["seq"] for r in first] == [r["seq"] for r in second] == [1, 2]


# ── dependent close-after-hub-drain stage ────────────────────────────────────

class _Callbacks:
    def __init__(self):
        self.closed = False

    def fence(self):
        pass

    def close(self, timeout=5.0):
        self.closed = True


class _Hub:
    """A hub whose close() fails a given number of times before succeeding."""

    def __init__(self, fail_times=0):
        self._fail = fail_times
        self.closes = 0

    def fence(self):
        pass

    def close(self):
        self.closes += 1
        if self._fail > 0:
            self._fail -= 1
            raise RuntimeError("hub drain incomplete")


class _Result:
    def __init__(self, resolved, lock_released):
        self.resolved = resolved
        self.lock_released = lock_released


class _History:
    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def close(self, timeout=5.0):
        self.calls += 1
        return self._results.pop(0)


def test_history_closes_only_after_the_hub_in_order():
    order = []

    class OrderHub(_Hub):
        def close(self):
            order.append("hub")

    class OrderHistory(_History):
        def close(self, timeout=5.0):
            order.append("history")
            return _Result(True, True)

    owner = WebRuntimeCleanup(_Callbacks(), OrderHub(), history=OrderHistory([]))
    owner.close()
    assert order == ["hub", "history"]
    assert owner.closed is True


def test_history_not_closed_until_hub_drains_then_retried():
    history = _History([_Result(True, True)])
    owner = WebRuntimeCleanup(_Callbacks(), _Hub(fail_times=1), history=history)
    with pytest.raises(BaseException):
        owner.close()               # hub drain fails -> the history stage is skipped
    assert history.calls == 0 and owner._history_done is False and owner.closed is False
    owner.close()                   # retry the exact stage: hub drains, THEN history closes
    assert history.calls == 1 and owner._history_done is True and owner.closed is True


def test_unresolved_history_close_keeps_same_owner_pending():
    history = _History([_Result(False, False), _Result(True, True)])
    owner = WebRuntimeCleanup(_Callbacks(), _Hub(), history=history)
    with pytest.raises(BaseException):
        owner.close()               # unresolved handle -> cleanup incomplete, not closed
    assert owner._hub_done is True and owner._history_done is False and owner.closed is False
    owner.close()                   # same history owner, retried, now resolved
    assert history.calls == 2 and owner._history_done is True and owner.closed is True


def test_absent_history_resolves_the_stage_immediately():
    owner = WebRuntimeCleanup(_Callbacks(), _Hub())   # 2-arg legacy construction, history=None
    owner.close()
    assert owner._history_done is True and owner.closed is True


def test_real_memory_history_close_is_resolved_after_hub():
    history = _started(rows=3)
    owner = WebRuntimeCleanup(_Callbacks(), _Hub(), history=history)
    owner.close()
    assert owner._history_done is True and owner.closed is True
