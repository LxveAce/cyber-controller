"""Adapter slice 1: passive BLE fact projection + an optional submit-only journal sink on TargetIngestor.

Pure projection unit tests plus ingestor integration against an explicitly caller-owned in-memory journal
and a scripted fake protocol. No disk journal, settings, routes, UI, parser or scanner/action is exercised.
"""
from __future__ import annotations

import pytest

from src.core.ble_fact_projection import project_addressed, project_report
from src.core.ble_journal import BleJournal, JournalLimits
from src.core.cross_comm import EventBus, TargetPool
from src.core.target_ingest import TargetIngestor
from src.protocols.base import ParsedEvent

_FOUND = "found-line"
_OBS = "obs-line"


class _FakeConn:
    def __init__(self, port):
        self.port = port
        self.is_connected = True
        self._cbs = []

    def on_line(self, cb):
        self._cbs.append(cb)

    def feed(self, line):
        for cb in list(self._cbs):
            cb(line)


class _FakeProto:
    """A protocol whose parse_line returns pre-scripted ParsedEvents keyed by the fed line."""

    def __init__(self, name, events):
        self._name = name
        self._events = dict(events)

    @property
    def protocol_name(self):
        return self._name

    def parse_line(self, line):
        return self._events.get(line)


def _found_event():
    return ParsedEvent(event_type="ble_found",
                       data={"mac": "AA:BB:CC:DD:EE:F0", "name": "Flipper", "rssi": -55, "type": "public"},
                       raw=_FOUND)


def _obs_event():
    return ParsedEvent(event_type="ble_observation",
                       data={"label": "BeaconX", "rssi": -70, "reported_index": None, "format": "live",
                             "label_truncated": False, "addressable": False}, raw=_OBS)


def _mem_journal():
    journal = BleJournal(persist_path=None)
    journal.start()
    return journal


def _rows(journal):
    return journal.read().rows


def _ingestor(journal, name="fake-fw", events=None):
    pool = TargetPool(EventBus())
    ing = TargetIngestor(pool, journal=journal)
    conn = _FakeConn("COM7")
    ing.attach(conn, _FakeProto(name, events if events is not None else {_FOUND: _found_event(),
                                                                         _OBS: _obs_event()}))
    return ing, conn, pool


# ── projection unit tests (pure) ─────────────────────────────────────────────────────────────────────

_SRC = {"port": "COM4", "firmware": "ghostesp", "connection_id": "1"}


def test_project_addressed_from_mac_lowercases_and_keeps_explicit_fields():
    fact = project_addressed({"mac": "AA:BB:CC:DD:EE:F0", "name": "Flipper", "rssi": -60, "type": "public"}, _SRC)
    assert fact == {"kind": "ble_found", "source": _SRC, "address": "aa:bb:cc:dd:ee:f0",
                    "address_type": "public", "label": "Flipper", "rssi": -60,
                    "report_meta": {}, "meta": {}}


def test_project_addressed_reads_addr_when_mac_absent():
    fact = project_addressed({"addr": "11:22:33:44:55:66"}, _SRC)
    assert fact["address"] == "11:22:33:44:55:66"
    assert fact["label"] == "" and fact["rssi"] is None and fact["address_type"] is None


def test_project_addressed_rejects_conflicting_and_malformed_and_absent_address():
    assert project_addressed({"mac": "AA:BB:CC:DD:EE:F0", "addr": "11:22:33:44:55:66"}, _SRC) is None
    for bad in ("not-a-mac", "AA:BB:CC:DD:EE", "AABBCCDDEEFF", "", "GG:BB:CC:DD:EE:FF"):
        assert project_addressed({"mac": bad}, _SRC) is None
    assert project_addressed({}, _SRC) is None
    assert project_addressed({"mac": "aa:bb:cc:dd:ee:f0", "addr": "AA:BB:CC:DD:EE:F0"}, _SRC) is not None


def test_project_addressed_rssi_null_distinct_from_zero_rejecting_bool_and_text():
    assert project_addressed({"mac": "AA:BB:CC:DD:EE:F0"}, _SRC)["rssi"] is None
    assert project_addressed({"mac": "AA:BB:CC:DD:EE:F0", "rssi": None}, _SRC)["rssi"] is None
    assert project_addressed({"mac": "AA:BB:CC:DD:EE:F0", "rssi": 0}, _SRC)["rssi"] == 0
    assert project_addressed({"mac": "AA:BB:CC:DD:EE:F0", "rssi": True}, _SRC) is None
    assert project_addressed({"mac": "AA:BB:CC:DD:EE:F0", "rssi": "-60"}, _SRC) is None


def test_project_addressed_only_maps_explicit_public_or_random_type():
    assert project_addressed({"mac": "AA:BB:CC:DD:EE:F0", "type": "random"}, _SRC)["address_type"] == "random"
    assert project_addressed({"mac": "AA:BB:CC:DD:EE:F0", "type": "weird"}, _SRC)["address_type"] is None
    assert project_addressed({"mac": "AA:BB:CC:DD:EE:F0"}, _SRC)["address_type"] is None


def test_project_report_maps_retained_snapshot_fields():
    retained = {"label": "Zynq", "rssi": -40, "reported_index": 3, "format": "list",
                "label_truncated": True, "scan_epoch": "7"}
    fact = project_report(retained, _SRC)
    assert fact == {"kind": "ble_observation", "source": _SRC, "address": None, "address_type": None,
                    "label": "Zynq", "rssi": -40,
                    "report_meta": {"reported_index": 3, "format": "list", "label_truncated": True,
                                    "scan_epoch": "7"}, "meta": {}}


def test_projections_never_raise_on_non_mapping_input():
    assert project_addressed(None, _SRC) is None
    assert project_report(None, _SRC) is None


# ── ingestor integration (fake protocol + explicitly-owned memory journal) ───────────────────────────

def test_default_no_sink_journals_nothing_and_keeps_live_routing():
    pool = TargetPool(EventBus())
    ing = TargetIngestor(pool)  # no journal
    conn = _FakeConn("COM7")
    ing.attach(conn, _FakeProto("fake-fw", {_FOUND: _found_event()}))
    conn.feed(_FOUND)
    assert ing.journal_status()["sink"] is False
    assert pool.count == 1  # the ble_found still reached the pool


def test_ble_found_is_journaled_once_with_captured_provenance():
    journal = _mem_journal()
    try:
        ing, conn, pool = _ingestor(journal)
        conn.feed(_FOUND)
        rows = _rows(journal)
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "ble_found" and row["address"] == "aa:bb:cc:dd:ee:f0"
        assert row["rssi"] == -55 and row["address_type"] == "public" and row["label"] == "Flipper"
        assert row["source"] == {"port": "COM7", "firmware": "fake-fw", "connection_id": "1"}
        assert pool.count == 1  # live pool still received it
        status = ing.journal_status()
        assert status["receipts"]["queued"] == 1 and status["last_result"] == "queued"
    finally:
        journal.close()


def test_ble_observation_is_journaled_and_live_report_history_kept():
    journal = _mem_journal()
    try:
        ing, conn, _ = _ingestor(journal)
        conn.feed(_OBS)
        rows = _rows(journal)
        assert len(rows) == 1 and rows[0]["kind"] == "ble_observation" and rows[0]["address"] is None
        assert rows[0]["report_meta"] == {"reported_index": None, "format": "live",
                                          "label_truncated": False, "scan_epoch": "1"}
        assert len(ing.ble_observations()) == 1  # the live in-memory report window is unaffected
    finally:
        journal.close()


def test_submit_is_at_most_once_per_eligible_event():
    journal = _mem_journal()
    try:
        _, conn, _ = _ingestor(journal)
        conn.feed(_FOUND)
        conn.feed(_FOUND)
        assert len(_rows(journal)) == 2  # exactly one row per event, never more
    finally:
        journal.close()


def test_raising_sink_leaves_live_routing_intact_and_never_retries():
    class _RaisingJournal:
        def __init__(self):
            self.calls = 0

        def submit(self, fact):
            self.calls += 1
            raise RuntimeError("sink boom")

    sink = _RaisingJournal()
    pool = TargetPool(EventBus())
    ing = TargetIngestor(pool, journal=sink)
    conn = _FakeConn("COM7")
    ing.attach(conn, _FakeProto("fake-fw", {_FOUND: _found_event()}))
    conn.feed(_FOUND)
    conn.feed(_FOUND)
    assert pool.count == 1  # live pool intact despite the raising sink
    assert sink.calls == 2  # one submit per event, never retried within an event
    assert ing.journal_status()["submit_exceptions"] == 2


def test_control_exception_from_sink_propagates_with_identity():
    class _KIJournal:
        def submit(self, fact):
            raise KeyboardInterrupt()

    pool = TargetPool(EventBus())
    ing = TargetIngestor(pool, journal=_KIJournal())
    conn = _FakeConn("COM7")
    ing.attach(conn, _FakeProto("fake-fw", {_FOUND: _found_event()}))
    with pytest.raises(KeyboardInterrupt):
        conn.feed(_FOUND)


def test_projection_rejection_is_counted_and_not_submitted():
    journal = _mem_journal()
    try:
        bad = ParsedEvent(event_type="ble_found", data={"mac": "nope", "rssi": -5}, raw="bad")
        ing, conn, _ = _ingestor(journal, events={"bad": bad})
        conn.feed("bad")
        assert len(_rows(journal)) == 0
        assert ing.journal_status()["projection_rejected"] == 1
    finally:
        journal.close()


def test_parser_switch_bumps_connection_epoch_in_provenance():
    journal = _mem_journal()
    try:
        pool = TargetPool(EventBus())
        ing = TargetIngestor(pool, journal=journal)
        conn = _FakeConn("COM7")
        ing.attach(conn, _FakeProto("fw-a", {_FOUND: _found_event()}))
        conn.feed(_FOUND)
        ing.attach(conn, _FakeProto("fw-b", {_FOUND: _found_event()}))  # switch on the same connection
        conn.feed(_FOUND)
        rows = _rows(journal)
        assert len(rows) == 2
        assert rows[0]["source"]["firmware"] == "fw-a" and rows[0]["source"]["connection_id"] == "1"
        assert rows[1]["source"]["firmware"] == "fw-b" and rows[1]["source"]["connection_id"] == "2"
    finally:
        journal.close()


# ── slice-1 boundary corrections (own-bound enforcement, strict addresses, exact-primitive receipts, immutable per-fact source, pre-route snapshot) ──

@pytest.mark.parametrize("name", ["x" * 513, "€" * 171, "\ud800", None, False],
                         ids=["ascii-over", "unicode-over", "invalid-utf8", "null", "bool"])
def test_invalid_or_oversized_name_rejected_not_offered(name):
    journal = _mem_journal()
    try:
        ev = ParsedEvent(event_type="ble_found",
                         data={"mac": "AA:BB:CC:DD:EE:F0", "name": name, "rssi": -60}, raw=_FOUND)
        ing, conn, pool = _ingestor(journal, events={_FOUND: ev})
        conn.feed(_FOUND)
        assert _rows(journal) == []                          # the invalid fact was never offered
        assert ing.journal_status()["projection_rejected"] == 1
        assert pool.count == 1                               # live pool routing unaffected
    finally:
        journal.close()


@pytest.mark.parametrize("rssi", [128, -129])
def test_out_of_range_rssi_rejected(rssi):
    assert project_addressed({"mac": "AA:BB:CC:DD:EE:F0", "rssi": rssi}, _SRC) is None


@pytest.mark.parametrize("bad", [{"port": "x" * 257, "firmware": "f", "connection_id": "1"},
                                 {"port": "p", "firmware": "x" * 257, "connection_id": "1"}])
def test_oversized_source_string_rejects(bad):
    assert project_addressed({"mac": "AA:BB:CC:DD:EE:F0", "rssi": -60}, bad) is None


@pytest.mark.parametrize("data", [{"mac": "AA:BB:CC:DD:EE:F0\n"},
                                  {"mac": None, "addr": "AA:BB:CC:DD:EE:F0"},
                                  {"mac": "AA:BB:CC:DD:EE:F0", "addr": None}],
                         ids=["trailing-newline", "present-null-mac", "present-null-addr"])
def test_full_string_match_and_every_present_field_validated(data):
    assert project_addressed(data, _SRC) is None


@pytest.mark.parametrize("receipt", [[], {}], ids=["list", "dict"])
def test_unhashable_receipt_counts_unknown_without_raising(receipt):
    class _Sink:
        def __init__(self):
            self.calls = 0

        def submit(self, fact):
            self.calls += 1
            return receipt

    sink = _Sink()
    pool = TargetPool(EventBus())
    ing = TargetIngestor(pool, journal=sink)
    conn = _FakeConn("COM7")
    ing.attach(conn, _FakeProto("fake-fw", {_FOUND: _found_event()}))
    conn.feed(_FOUND)
    conn.feed(_FOUND)
    status = ing.journal_status()
    assert status["receipts"]["submission_unknown"] == 2
    assert status["last_result"] == "submission_unknown" and status["submit_exceptions"] == 0
    assert sink.calls == 2


def test_sink_mutation_of_source_cannot_affect_a_later_fact():
    class _Mutating:
        def __init__(self):
            self.sources = []

        def submit(self, fact):
            self.sources.append(dict(fact["source"]))   # snapshot before mutating
            fact["source"]["firmware"] = "mutated"
            fact["source"]["foreign"] = "x"
            return "queued"

    sink = _Mutating()
    pool = TargetPool(EventBus())
    ing = TargetIngestor(pool, journal=sink)
    conn = _FakeConn("COM7")
    ing.attach(conn, _FakeProto("fake-fw", {_FOUND: _found_event()}))
    conn.feed(_FOUND)
    conn.feed(_FOUND)
    expected = {"port": "COM7", "firmware": "fake-fw", "connection_id": "1"}
    assert sink.sources == [expected, expected]


def test_addressed_fact_snapshotted_before_reentrant_pool_callback():
    event = _found_event()  # name "Flipper"
    journal = _mem_journal()
    try:
        bus = EventBus()
        pool = TargetPool(bus)
        ing = TargetIngestor(pool, journal=journal)
        conn = _FakeConn("COM7")
        ing.attach(conn, _FakeProto("fake-fw", {_FOUND: event}))
        bus.subscribe("target.added", lambda topic, payload: event.data.__setitem__("name", "rewritten"))
        conn.feed(_FOUND)
        assert pool.all()[0].ssid == "Flipper"           # the pooled target kept the pre-callback name
        assert _rows(journal)[0]["label"] == "Flipper"   # the journaled fact was snapshotted before _route
    finally:
        journal.close()


def test_smaller_caller_limit_is_a_separate_core_rejection():
    journal = BleJournal(persist_path=None, limits=JournalLimits(max_label_bytes=3))
    journal.start()
    try:
        ing, conn, pool = _ingestor(journal)
        conn.feed(_FOUND)  # name "Flipper" is valid by the adapter default but exceeds the core cap of 3
        status = ing.journal_status()
        assert status["projection_rejected"] == 0
        assert status["receipts"]["rejected:invalid"] == 1 and pool.count == 1
    finally:
        journal.close()


def test_str_subclass_receipt_never_invokes_custom_hash_and_counts_unknown():
    # A str subclass may carry a custom __hash__/__eq__; the exact-primitive receipt contract
    # classifies it as unknown WITHOUT invoking that code and WITHOUT coercion or retry -- only a
    # genuine str participates in the known-receipt membership test.
    effects = []

    class _Hashing(str):
        def __hash__(self):
            effects.append("hash")
            return 0

        def __eq__(self, other):
            effects.append("eq")
            return True

    receipt = _Hashing("queued")  # equals a KNOWN receipt string, but is not an exact str

    class _Sink:
        def submit(self, fact):
            return receipt

    pool = TargetPool(EventBus())
    ing = TargetIngestor(pool, journal=_Sink())
    conn = _FakeConn("COM7")
    ing.attach(conn, _FakeProto("fake-fw", {_FOUND: _found_event()}))
    conn.feed(_FOUND)
    status = ing.journal_status()
    assert status["receipts"]["submission_unknown"] == 1     # not misclassified as the known "queued"
    assert status["receipts"]["queued"] == 0
    assert status["last_result"] == "submission_unknown" and status["submit_exceptions"] == 0
    assert effects == []                                     # the subclass hash/eq was never executed


@pytest.mark.parametrize("mutated_kind", ["device_info", "ble_observation"])
def test_addressed_fact_survives_event_type_mutation_during_routing(mutated_kind):
    # A pool callback that rewrites event_type mid-route (to device_info or ble_observation) must not
    # discard the addressed fact captured from the pristine event: the single later journal offer is
    # driven by the kind captured before re-entrant routing, not the post-route event_type.
    event = _found_event()  # ble_found, name "Flipper"
    journal = _mem_journal()
    try:
        bus = EventBus()
        pool = TargetPool(bus)
        ing = TargetIngestor(pool, journal=journal)
        conn = _FakeConn("COM7")
        ing.attach(conn, _FakeProto("fake-fw", {_FOUND: event}))
        bus.subscribe("target.added", lambda topic, payload: setattr(event, "event_type", mutated_kind))
        conn.feed(_FOUND)
        rows = _rows(journal)
        assert len(rows) == 1                                # the captured addressed fact, not dropped
        assert rows[0]["label"] == "Flipper"
        assert ing.journal_status()["projection_rejected"] == 0   # no spurious None offer from a reread kind
    finally:
        journal.close()
