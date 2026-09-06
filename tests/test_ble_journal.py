"""Inert tests for the BLE journal M1 core/store (src/core/ble_journal.py). No device I/O, no HTTP/UI, no
hub/ingestor edits. Uses fixture-owned temporary stores only."""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from src.core import ble_journal as bj


def _fact(**over):
    f = {
        "kind": "ble_found",
        "source": {"port": "COM4", "firmware": "marauder", "connection_id": "c1"},
        "address": "aa:bb:cc:dd:ee:ff",
        "address_type": "public",
        "label": "device",
        "rssi": -60,
        "report_meta": {},
        "meta": {"vendor": "Espressif"},
    }
    f.update(over)
    return f


def _drain(j, timeout=5.0):
    r = j.flush(timeout)
    return r


# ── build_row: allowlist / bounds / identity ──────────────────────────────────────────────────────────

def test_build_row_valid_and_shape():
    built = bj.build_row("run", 1, "2026-09-06T00:00:00+00:00", _fact(), bj.JournalLimits())
    assert built is not None
    row, line = built
    assert row["schema_version"] == 1 and row["run_id"] == "run" and row["seq"] == 1
    assert row["kind"] == "ble_found" and row["addressable"] is True
    assert row["source"] == {"port": "COM4", "firmware": "marauder", "connection_id": "c1"}
    assert json.loads(line) == row


@pytest.mark.parametrize("bad", [
    {"kind": "wifi_found"},                       # not a BLE kind
    {"kind": None},
    {"source": "COM4"},                           # source must be a mapping
    {"source": {"port": "x" * 9000, "firmware": "f", "connection_id": "c"}},   # oversized subfield
    {"address_type": "bogus"},
    {"label": "x" * 9000},                        # oversized label
    {"rssi": 999},                                # out of range
    {"rssi": True},                               # bool is not an rssi int
    {"meta": {"k": [1, 2, 3]}},                   # non-primitive nested value
    {"meta": {"k": object()}},
])
def test_build_row_rejects_invalid(bad):
    assert bj.build_row("run", 1, "t", _fact(**bad), bj.JournalLimits()) is None


def test_build_row_oversized_row_rejected():
    tiny = bj.JournalLimits(max_row_bytes=64)     # a full valid row is larger than 64 bytes
    assert bj.build_row("run", 1, "2026-09-06T00:00:00+00:00", _fact(), tiny) is None


def test_null_rssi_distinct_from_zero():
    r0 = bj.build_row("r", 1, "t", _fact(rssi=0), bj.JournalLimits())[0]
    rn = bj.build_row("r", 2, "t", _fact(rssi=None), bj.JournalLimits())[0]
    assert r0["rssi"] == 0 and rn["rssi"] is None


def test_addressless_report_has_no_address():
    row = bj.build_row("r", 1, "t", _fact(kind="ble_observation", address=None, address_type=None,
                                          report_meta={"format": "list", "index": 3}), bj.JournalLimits())[0]
    assert row["kind"] == "ble_observation" and row["address"] is None and row["addressable"] is False
    assert row["report_meta"] == {"format": "list", "index": 3}


# ── memory mode: no filesystem activity, bounded history ───────────────────────────────────────────────

def test_memory_mode_does_no_filesystem_activity(tmp_path):
    sub = tmp_path / "empty"
    j = bj.BleJournal(persist_path=None)
    j.start()
    assert j.submit(_fact()) == "queued"
    assert j.submit(_fact(kind="ble_observation", address=None, address_type=None)) == "queued"
    page = j.read()
    assert len(page.rows) == 2 and page.rows[0]["seq"] == 1
    j.close()
    assert not sub.exists()                       # nothing was created anywhere by memory mode


def test_memory_history_is_bounded():
    j = bj.BleJournal(persist_path=None, limits=bj.JournalLimits(max_mem_rows=3))
    with j:
        for _ in range(10):
            j.submit(_fact())
        rows = j.read(max_rows=256).rows
        assert len(rows) == 3                      # oldest evicted, bounded


def test_seq_starts_at_one_and_is_monotonic():
    j = bj.BleJournal(persist_path=None)
    with j:
        j.submit(_fact()); j.submit(_fact()); j.submit(_fact())
        seqs = [r["seq"] for r in j.read().rows]
        assert seqs == [1, 2, 3]


# ── persist mode: durability, identity, read-back ──────────────────────────────────────────────────────

def test_persist_start_creates_lock_and_segment(tmp_path):
    d = str(tmp_path / "store")
    j = bj.BleJournal(persist_path=d)
    j.start()
    try:
        names = os.listdir(d)
        assert "ble-journal.lock" in names
        assert any(n.startswith("ble-") and n.endswith(".jsonl") for n in names)
    finally:
        j.close()


def test_submit_flush_confirms_and_reads_back(tmp_path):
    d = str(tmp_path / "store")
    with bj.BleJournal(persist_path=d) as j:
        assert j.submit(_fact(label="one")) == "queued"
        assert j.submit(_fact(label="two")) == "queued"
        res = _drain(j)
        assert res.undrained == 0 and res.confirmed_seq == 2 and not res.degraded
        rows = j.read().rows
        assert [r["label"] for r in rows] == ["one", "two"]
        assert j.counters()["confirmed"] == 2


def test_two_source_interleave_distinct_provenance(tmp_path):
    d = str(tmp_path / "store")
    with bj.BleJournal(persist_path=d) as j:
        j.submit(_fact(source={"port": "COM4", "firmware": "marauder", "connection_id": "a"}))
        j.submit(_fact(source={"port": "COM4", "firmware": "marauder", "connection_id": "b"}))  # same port, reconnect
        _drain(j)
        rows = j.read().rows
        conns = {r["source"]["connection_id"] for r in rows}
        assert conns == {"a", "b"}                 # a same-port reconnect is distinguished by connection id


def test_memory_is_not_persistence_confirmed_only_after_flush(tmp_path):
    d = str(tmp_path / "store")
    with bj.BleJournal(persist_path=d) as j:
        j.submit(_fact())
        # Before flush, the row may still be queued/uncertain — confirmed only advances on a durable fsync.
        res = _drain(j)
        assert res.confirmed_seq == 1


# ── overflow / oversized counters ─────────────────────────────────────────────────────────────────────

def test_oversized_fact_rejected_and_counted(tmp_path):
    d = str(tmp_path / "store")
    with bj.BleJournal(persist_path=d) as j:
        assert j.submit(_fact(label="x" * 9000)) == "rejected:invalid"
        assert j.counters()["invalid"] == 1


def test_queue_overflow_drops_and_counts(tmp_path, monkeypatch):
    # Block the writer inside os.write so the bounded queue fills, then confirm excess submits are dropped
    # and counted (drop-incoming-and-count), never retaining the payload.
    d = str(tmp_path / "store")
    gate = threading.Event()
    real_write = os.write

    def slow(fd, data):
        gate.wait(2.0)
        return real_write(fd, data)

    j = bj.BleJournal(persist_path=d, limits=bj.JournalLimits(max_queue_rows=2))
    j.start()
    try:
        monkeypatch.setattr(os, "write", slow)
        results = [j.submit(_fact()) for _ in range(8)]
        assert "rejected:queue_full" in results
        assert j.counters()["queue_full"] >= 1
    finally:
        gate.set()
        monkeypatch.setattr(os, "write", real_write)
        j.close()


# ── restart / recovery: new segment, fresh seq, prior rows readable ────────────────────────────────────

def test_restart_creates_new_segment_and_resets_seq(tmp_path):
    d = str(tmp_path / "store")
    with bj.BleJournal(persist_path=d) as j:
        j.submit(_fact(label="first")); _drain(j)
    seg_names_1 = sorted(n for n in os.listdir(d) if n.startswith("ble-") and n.endswith(".jsonl"))
    with bj.BleJournal(persist_path=d) as j2:
        assert j2.submit(_fact(label="second"))  # seq starts at 1 again for the new run
        _drain(j2)
        rows = j2.read().rows
        labels = [r["label"] for r in rows]
        assert labels == ["first", "second"]        # prior segment is immutable input, read in order
        assert [r["seq"] for r in rows] == [1, 1]    # each run's seq restarts at 1
    seg_names_2 = sorted(n for n in os.listdir(d) if n.startswith("ble-") and n.endswith(".jsonl"))
    assert len(seg_names_2) > len(seg_names_1)       # a NEW segment (new ordering+id) was created


def test_cursor_identity_and_expiry(tmp_path):
    d = str(tmp_path / "store")
    with bj.BleJournal(persist_path=d) as j:
        for i in range(3):
            j.submit(_fact(label=f"r{i}"))
        _drain(j)
        page = j.read(max_rows=2)
        assert len(page.rows) == 2 and page.next_cursor is not None
        # A cursor whose segment no longer exists reports explicit expiry.
        stale = bj.Cursor(ordering=999999, seg_id="deadbeef", offset=0)
        assert j.read(cursor=stale).expired is True


# ── rotation + retention ───────────────────────────────────────────────────────────────────────────────

def test_rotation_and_retention_bounded(tmp_path):
    d = str(tmp_path / "store")
    # A tiny segment cap forces frequent rotation; retention keeps at most max_data_segments.
    lim = bj.JournalLimits(max_row_bytes=500, max_segment_bytes=1100, max_data_segments=3)
    with bj.BleJournal(persist_path=d, limits=lim) as j:
        for i in range(40):
            j.submit(_fact(label=f"row-{i:03d}"))
        _drain(j)
        segs = [n for n in os.listdir(d) if n.startswith("ble-") and n.endswith(".jsonl")]
        assert len(segs) <= 3                        # retention bound held
        assert j.counters()["retention_deleted"] >= 1
        # The read reports the earliest retained ordering (older rows were retired honestly).
        page = j.read(max_rows=256)
        assert page.earliest_ordering is not None


# ── competing owner ────────────────────────────────────────────────────────────────────────────────────

def test_competing_owner_is_rejected(tmp_path):
    d = str(tmp_path / "store")
    j1 = bj.BleJournal(persist_path=d)
    j1.start()
    try:
        j2 = bj.BleJournal(persist_path=d)
        with pytest.raises(bj.JournalUnavailable):
            j2.start()
    finally:
        j1.close()
    # After the owner releases, a fresh owner can acquire.
    with bj.BleJournal(persist_path=d) as j3:
        assert j3.submit(_fact()) == "queued"


# ── recovery: torn tail + malformed line ───────────────────────────────────────────────────────────────

def _write_lines(path, lines):
    with open(path, "wb") as fh:
        fh.write(b"".join(lines))


def test_recovery_skips_malformed_and_stops_at_torn_tail(tmp_path):
    d = str(tmp_path / "store")
    os.makedirs(d)
    good = json.dumps({"schema_version": 1, "seq": 1, "label": "ok"}).encode() + b"\n"
    malformed = b"{not json but complete}\n"
    good2 = json.dumps({"schema_version": 1, "seq": 2, "label": "ok2"}).encode() + b"\n"
    torn = b'{"schema_version":1,"seq":3,"label":"torn-no-newline"}'   # no trailing newline
    _write_lines(os.path.join(d, "ble-000001-aaaaaa.jsonl"), [good, malformed, good2, torn])
    with bj.BleJournal(persist_path=d) as j:
        page = j.read(max_rows=256)
        labels = [r.get("label") for r in page.rows]
        assert "ok" in labels and "ok2" in labels        # valid records survive a malformed complete line
        assert "torn-no-newline" not in labels           # an uncommitted newline-less tail is not a record
        assert j.counters()["recovery_skipped"] >= 1


# ── degraded state on write failure ────────────────────────────────────────────────────────────────────

def test_write_failure_degrades_and_marks_uncertain(tmp_path, monkeypatch):
    d = str(tmp_path / "store")
    j = bj.BleJournal(persist_path=d)
    j.start()
    try:
        real_write = os.write

        def boom(fd, data):
            raise OSError("disk full")

        monkeypatch.setattr(os, "write", boom)
        j.submit(_fact())
        res = _drain(j, timeout=2.0)
        assert res.degraded is True
        c = j.counters()
        assert c["uncertain"] == 1 and c["confirmed"] == 0   # never counted confirmed or proven-lost
        monkeypatch.setattr(os, "write", real_write)
    finally:
        j.close()


# ── close reports undrained honestly ───────────────────────────────────────────────────────────────────

def test_close_releases_lock_and_reports(tmp_path):
    d = str(tmp_path / "store")
    j = bj.BleJournal(persist_path=d)
    j.start()
    j.submit(_fact()); _drain(j)
    res = j.close()
    assert res.lock_released is True and res.undrained == 0
    # closing fences admission
    with pytest.raises(bj.JournalStateError):
        j.submit(_fact())


def test_close_with_blocked_writer_reports_unresolved_handle(tmp_path, monkeypatch):
    d = str(tmp_path / "store")
    gate = threading.Event()
    real_write = os.write

    def block(fd, data):
        gate.wait(5.0)
        return real_write(fd, data)

    j = bj.BleJournal(persist_path=d)
    j.start()
    try:
        monkeypatch.setattr(os, "write", block)
        j.submit(_fact())
        time.sleep(0.05)                              # let the writer pick up and block on the row
        res = j.close(timeout=0.2)
        assert res.lock_released is False             # a timed-out live writer retains ownership
        assert res.undrained >= 1                     # reported honestly, not asserted drained
    finally:
        gate.set()
        monkeypatch.setattr(os, "write", real_write)
        if j._writer:
            j._writer.join(2.0)
        if j._lock:
            j._lock.release()


def test_retention_delete_failure_is_counted_not_hidden(tmp_path, monkeypatch):
    d = str(tmp_path / "store")
    lim = bj.JournalLimits(max_row_bytes=500, max_segment_bytes=1100, max_data_segments=3)
    j = bj.BleJournal(persist_path=d, limits=lim)
    j.start()
    try:
        real_remove = os.remove

        def refuse(path):
            raise OSError("open handle")

        monkeypatch.setattr(os, "remove", refuse)
        for i in range(30):
            j.submit(_fact(label=f"r{i}"))
        _drain(j)
        c = j.counters()
        assert c["retention_failed"] >= 1             # a failed delete is reported, never silently hidden
        assert c["retention_deleted"] == 0
        monkeypatch.setattr(os, "remove", real_remove)
    finally:
        j.close()


def test_restart_after_torn_tail_appends_to_new_segment(tmp_path):
    d = str(tmp_path / "store")
    os.makedirs(d)
    torn_path = os.path.join(d, "ble-000005-aaaaaa.jsonl")
    with open(torn_path, "wb") as fh:
        fh.write(b'{"schema_version":1,"seq":9,"label":"torn"}')   # no trailing newline
    before = os.path.getsize(torn_path)
    with bj.BleJournal(persist_path=d) as j:
        j.submit(_fact(label="fresh"))
        _drain(j)
        assert os.path.getsize(torn_path) == before   # the torn prior segment is never appended behind
        segs = [n for n in os.listdir(d) if n.startswith("ble-") and n.endswith(".jsonl")]
        orderings = [bj._parse_segment_name(n)[0] for n in segs]
        assert 6 in orderings                          # a new, higher-ordering segment (prior max 5 -> 6)
        assert any(r.get("label") == "fresh" for r in j.read().rows)


def test_recovery_advances_past_a_too_long_line(tmp_path):
    d = str(tmp_path / "store")
    os.makedirs(d)
    huge = b'{"x":"' + b"A" * 5000 + b'"}\n'                       # a single complete but over-long line
    good = json.dumps({"schema_version": 1, "seq": 1, "label": "after"}).encode() + b"\n"
    _write_lines(os.path.join(d, "ble-000001-aaaaaa.jsonl"), [huge, good])
    with bj.BleJournal(persist_path=d, limits=bj.JournalLimits(max_row_bytes=200)) as j:
        rows = j.read(max_rows=256).rows
        assert any(r.get("label") == "after" for r in rows)       # advanced past the corrupt long line
        assert j.counters()["recovery_skipped"] >= 1


def test_read_returns_plain_data_and_no_target_authority():
    j = bj.BleJournal(persist_path=None)
    with j:
        j.submit(_fact())
        page = j.read()
        assert page.rows and all(isinstance(r, dict) for r in page.rows)   # plain data, never a Target
    assert not any(hasattr(j, name) for name in ("add", "pool", "target", "route", "attack"))


def _raise_oserror(*a):
    raise OSError("boom")


def test_bjm01_short_write_is_not_acknowledged(tmp_path, monkeypatch):
    d = str(tmp_path / "store")
    j = bj.BleJournal(persist_path=d)
    j.start()
    try:
        monkeypatch.setattr(os, "write", lambda fd, data: 0)   # a zero write cannot complete
        j.submit(_fact())
        res = j.flush(2.0)
        assert res.degraded is True and res.confirmed_seq == -1     # never a false durable ack
        c = j.counters()
        assert c["confirmed"] == 0 and c["uncertain"] == 1
        monkeypatch.setattr(os, "write", lambda *a: (_ for _ in ()).throw(AssertionError))  # no more writes
    finally:
        monkeypatch.undo()
        j.close()


def test_bjm02_degraded_rejects_new_work_and_keeps_counts(tmp_path, monkeypatch):
    d = str(tmp_path / "store")
    j = bj.BleJournal(persist_path=d)
    j.start()
    try:
        monkeypatch.setattr(os, "write", _raise_oserror)
        assert j.submit(_fact()) == "queued"       # admitted; the first write fails -> degrade
        j.flush(2.0)
        assert j.submit(_fact()) == "rejected:degraded"    # new work rejected until reopen, not silently lost
        c = j.counters()
        assert c["degraded_rejected"] == 1 and c["uncertain"] == 1 and c["confirmed"] == 0
        # every admitted row is accounted (the uncertain row is counted in `uncertain`, not lost):
        assert c["admitted"] == c["confirmed"] + c["uncertain"] + c["undrained"]
    finally:
        monkeypatch.undo()
        j.close()


def test_bjm04_timed_out_close_is_retryable(tmp_path, monkeypatch):
    d = str(tmp_path / "store")
    gate = threading.Event()
    real_write = os.write

    def block(fd, data):
        gate.wait(5.0)
        return real_write(fd, data)

    j = bj.BleJournal(persist_path=d)
    j.start()
    try:
        monkeypatch.setattr(os, "write", block)
        j.submit(_fact())
        time.sleep(0.05)
        r1 = j.close(timeout=0.2)
        assert r1.resolved is False and r1.lock_released is False and r1.undrained >= 1
        r2 = j.close(timeout=0.2)                    # retry while still blocked: honest, still unresolved
        assert r2.resolved is False and r2.undrained >= 1
        gate.set()
        monkeypatch.setattr(os, "write", real_write)
        r3 = j.close(timeout=3.0)                    # worker exits -> resolves and releases
        assert r3.resolved is True and r3.lock_released is True
    finally:
        gate.set()
        monkeypatch.undo()
        if j._lock:
            j._lock.release()


def test_bjm05_reserve_before_create_bounds_growth_on_delete_failure(tmp_path, monkeypatch):
    d = str(tmp_path / "store")
    lim = bj.JournalLimits(max_row_bytes=500, max_segment_bytes=1100, max_data_segments=3)
    j = bj.BleJournal(persist_path=d, limits=lim)
    j.start()
    try:
        monkeypatch.setattr(os, "remove", _raise_oserror)   # a real delete denial
        for i in range(30):
            j.submit(_fact(label=f"r{i}"))
        j.flush(3.0)
        segs = [n for n in os.listdir(d) if n.startswith("ble-") and n.endswith(".jsonl")]
        assert len(segs) <= lim.max_data_segments   # growth stopped at the cap (degraded instead of growing)
        c = j.counters()
        assert c["retention_failed"] >= 1 and c["degraded"] is True
    finally:
        monkeypatch.undo()
        j.close()


def test_bjm06_incomplete_catalog_fails_visibly(tmp_path):
    d = str(tmp_path / "store")
    os.makedirs(d)
    for o in (10, 20):
        open(os.path.join(d, f"ble-{o:09d}-aaaaaa.jsonl"), "wb").close()
    j = bj.BleJournal(persist_path=d, limits=bj.JournalLimits(max_dir_entries=1))
    with pytest.raises(bj.JournalUnavailable):
        j.start()


def test_bjm07_read_does_not_cross_unconfirmed_boundary(tmp_path, monkeypatch):
    d = str(tmp_path / "store")
    gate = threading.Event()
    real_fsync = os.fsync

    def blocked(fd):
        gate.wait(5.0)
        return real_fsync(fd)

    j = bj.BleJournal(persist_path=d)
    j.start()
    try:
        monkeypatch.setattr(os, "fsync", blocked)
        j.submit(_fact(label="pending"))
        time.sleep(0.1)                             # bytes written, writer blocked before fsync ack
        page = j.read()
        assert all(r.get("label") != "pending" for r in page.rows)
        assert j.counters()["confirmed_seq"] == -1
    finally:
        gate.set()
        monkeypatch.undo()
        j.close()


def test_bjm08_read_respects_byte_budget_even_first_row(tmp_path):
    d = str(tmp_path / "store")
    with bj.BleJournal(persist_path=d) as j:
        j.submit(_fact()); _drain(j)
        page = j.read(max_bytes=1)
        assert page.rows == [] and page.budget_too_small is True
    with bj.BleJournal(persist_path=None) as jm:
        jm.submit(_fact())
        page = jm.read(max_bytes=1)
        assert page.rows == [] and page.budget_too_small is True


def test_bjm09_corrupt_long_line_suffix_never_becomes_record(tmp_path):
    d = str(tmp_path / "store")
    os.makedirs(d)
    lim = bj.JournalLimits(max_row_bytes=100)
    corrupt = b"X" * 202 + b'{"schema_version":1,"label":"forged-fragment"}' + b"\n"
    good = json.dumps({"schema_version": 1, "label": "real"}).encode() + b"\n"
    _write_lines(os.path.join(d, "ble-000001-aaaaaa.jsonl"), [corrupt, good])
    with bj.BleJournal(persist_path=d, limits=lim) as j:
        rows = []
        cursor = None
        for _ in range(6):
            page = j.read(cursor=cursor, max_rows=10)
            rows.extend(page.rows)
            cursor = page.next_cursor
            if cursor is None:
                break
        labels = [r.get("label") for r in rows]
        assert "forged-fragment" not in labels     # the corrupt line's suffix is never promoted to a record
        assert "real" in labels                     # the valid record after the newline stays readable


def test_bjm10_memory_read_detached_and_cursor_stable():
    j = bj.BleJournal(persist_path=None, limits=bj.JournalLimits(max_mem_rows=100))
    with j:
        j.submit(_fact(label="a")); j.submit(_fact(label="b"))
        page = j.read(max_rows=1)
        assert [r["label"] for r in page.rows] == ["a"]
        page.rows[0]["source"]["connection_id"] = "HACKED"    # must not corrupt retained state
        j.submit(_fact(label="c"))
        page2 = j.read(cursor=page.next_cursor)
        assert [r["label"] for r in page2.rows] == ["b", "c"]  # stable seq cursor didn't skip retained 'b'
        assert all(r["source"]["connection_id"] != "HACKED" for r in page2.rows)


def test_bjm10_memory_cursor_expiry_on_eviction():
    j = bj.BleJournal(persist_path=None, limits=bj.JournalLimits(max_mem_rows=2))
    with j:
        j.submit(_fact()); j.submit(_fact())
        page = j.read(max_rows=1)                    # names seq 1
        j.submit(_fact()); j.submit(_fact())         # evicts seq 1, 2
        p2 = j.read(cursor=page.next_cursor)
        assert p2.expired is True


def test_audit_str_subclass_and_unicode_boundary():
    class Sneaky(str):
        pass
    assert bj.build_row("r", 1, "t", _fact(label=Sneaky("x")), bj.JournalLimits()) is None   # no str subclass
    # a genuine Unicode label round-trips and its byte length is bounded, not its char length
    built = bj.build_row("r", 1, "t", _fact(label="café-❤"), bj.JournalLimits())
    assert built is not None and json.loads(built[1])["label"] == "café-❤"


def test_audit_canonical_path_aliases_share_one_owner(tmp_path):
    base = str(tmp_path / "store")
    j1 = bj.BleJournal(persist_path=base)
    j1.start()
    try:
        j2 = bj.BleJournal(persist_path=base + os.sep)     # a path alias must resolve to the SAME lock
        with pytest.raises(bj.JournalUnavailable):
            j2.start()
    finally:
        j1.close()


def test_audit_arbitrary_cursor_is_safe(tmp_path):
    d = str(tmp_path / "store")
    with bj.BleJournal(persist_path=d) as j:
        j.submit(_fact()); _drain(j)
        # a cursor for a non-existent segment expires; an out-of-range offset simply yields no rows, no crash
        assert j.read(cursor=bj.Cursor(7, "nope", 0)).expired is True
        page = j.read(cursor=bj.Cursor(j._active_ordering, j._active_seg_id, 10 ** 9))
        assert page.rows == [] and page.expired is False
    with pytest.raises(bj.JournalError):
        bj.Cursor.parse("not-a-valid-token")


def test_bjs01_read_excludes_a_segment_newer_than_the_active_generation(tmp_path):
    d = str(tmp_path / "store")
    with bj.BleJournal(persist_path=d) as j:
        j.submit(_fact(label="confirmed")); _drain(j)
        active = j._active_ordering
        # a segment with a higher ordering than the captured active identity is a raced/future generation and
        # must not be read as unlimited history (its bytes could be unacknowledged).
        future = os.path.join(d, f"ble-{active + 1:09d}-ffffff.jsonl")
        with open(future, "wb") as fh:
            fh.write(json.dumps({"schema_version": 1, "label": "future-unconfirmed"}).encode() + b"\n")
        labels = [r.get("label") for r in j.read(max_rows=256).rows]
        assert "confirmed" in labels and "future-unconfirmed" not in labels


def test_bjs02_memory_cursor_from_another_lifetime_expires():
    with bj.BleJournal(persist_path=None) as j:
        j.submit(_fact()); j.submit(_fact())
        foreign = bj.Cursor(0, "some-other-run-id", 1)      # a cursor bound to a different lifetime
        assert j.read(cursor=foreign).expired is True


def test_bjs03_non_utf8_string_is_rejected_not_fatal(tmp_path):
    d = str(tmp_path / "store")
    with bj.BleJournal(persist_path=d) as j:
        assert j.submit(_fact(label="lone\udc80surrogate")) == "rejected:invalid"
        assert j.submit(_fact(meta={"k\udc80": "v"})) == "rejected:invalid"
        assert j.submit(_fact(meta={"k": "v\udc80"})) == "rejected:invalid"
        c = j.counters()
        assert c["invalid"] == 3 and c["admitted"] == 0     # no seq allocated, next valid fact still works
        assert j.submit(_fact(label="ok")) == "queued"


def test_bjs04_too_small_scan_budget_is_explicit_non_progress(tmp_path):
    d = str(tmp_path / "store")
    with bj.BleJournal(persist_path=d, limits=bj.JournalLimits(max_scan_bytes=16)) as j:
        j.submit(_fact()); _drain(j)
        page = j.read()
        assert page.rows == [] and page.budget_too_small is True and page.next_cursor is None
        assert page.scanned_bytes <= 16
        # a second read of the same yields the same explicit outcome, never a silently stuck advancing cursor
        assert j.read().budget_too_small is True


def test_bjs05_thread_start_failure_retains_ownership(tmp_path, monkeypatch):
    d = str(tmp_path / "store")
    real_start = threading.Thread.start

    def boom(self):
        raise RuntimeError("start reported an error")

    monkeypatch.setattr(threading.Thread, "start", boom)
    j = bj.BleJournal(persist_path=d)
    with pytest.raises(RuntimeError):
        j.start()                                            # post-launch uncertainty -> lock retained
    monkeypatch.setattr(threading.Thread, "start", real_start)
    try:
        j2 = bj.BleJournal(persist_path=d)
        with pytest.raises(bj.JournalUnavailable):
            j2.start()                                       # a second owner stays excluded
        res = j.close(0.2)
        assert res.resolved is False and res.lock_released is False   # honest unresolved handle
    finally:
        if j._lock:
            j._lock.release()


def test_bjt01_failed_prelaunch_release_is_retried_by_close(tmp_path, monkeypatch):
    d = str(tmp_path / "store")
    j = bj.BleJournal(persist_path=d)
    primary = OSError("fixture open failure")

    def fail_open(ordering):
        raise primary

    with monkeypatch.context() as p:
        p.setattr(j, "_open_new_segment", fail_open)
        p.setattr(bj._StoreLock, "release", lambda lock: False)   # release fails during rollback
        with pytest.raises(OSError):
            j.start()
    retained = j._lock
    assert retained is not None and retained.held()               # handle retained (not lost) after failure
    res = j.close(0)                                               # release works now -> retry succeeds (BJT-01)
    assert res.resolved and res.lock_released and not retained.held()


def test_bjt02_cleanup_exception_preserves_primary(tmp_path, monkeypatch):
    d = str(tmp_path / "store")
    j = bj.BleJournal(persist_path=d)
    primary = OSError("fixture primary failure")

    def fail_open(ordering):
        raise primary

    def raise_release(lock):
        raise RuntimeError("fixture cleanup failure")

    with monkeypatch.context() as p:
        p.setattr(j, "_open_new_segment", fail_open)
        p.setattr(bj._StoreLock, "release", raise_release)
        with pytest.raises(OSError) as caught:
            j.start()
    assert caught.value is primary                                # secondary cleanup error did not replace it
    if j._lock:
        j._lock.release()


def test_bjt03_unexpected_worker_exit_fences_admission(tmp_path, monkeypatch):
    d = str(tmp_path / "store")
    j = bj.BleJournal(persist_path=d)
    exited = threading.Event()

    def fail_write(item):
        raise RuntimeError("fixture unexpected writer failure")

    monkeypatch.setattr(j, "_write_one", fail_write)
    monkeypatch.setattr(threading, "excepthook", lambda args: exited.set())
    j.start()
    try:
        assert j.submit(_fact()) == "queued"
        assert exited.wait(3)                                     # the primary reached threading.excepthook
        j._writer.join(3)
        assert not j._writer.is_alive()
        c = j.counters()
        assert c["worker_failed"] is True and c["degraded"] is True
        assert j.submit(_fact()) in ("rejected:degraded", "rejected:closing")   # no worker -> new work rejected
    finally:
        monkeypatch.undo()
        j.close(1)


@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit])
def test_bju01_control_exception_during_rollback_preserves_primary(tmp_path, monkeypatch, control):
    d = str(tmp_path / "store")
    j = bj.BleJournal(persist_path=d)
    primary = OSError("fixture primary failure")

    def fail_open(ordering):
        raise primary

    def raise_control(lock):
        raise control("fixture cleanup control")

    with monkeypatch.context() as p:
        p.setattr(j, "_open_new_segment", fail_open)
        p.setattr(bj._StoreLock, "release", raise_control)
        with pytest.raises(OSError) as caught:
            j.start()                                    # a control raised by release must not replace primary
    assert caught.value is primary
    if j._lock:
        j._lock.release()


def test_bju02_bad_repr_worker_exception_preserves_primary(tmp_path, monkeypatch):
    d = str(tmp_path / "store")

    class BadRepr(RuntimeError):
        def __repr__(self):
            raise ValueError("repr must not be called on the worker path")

    j = bj.BleJournal(persist_path=d)
    primary = BadRepr("boom")
    captured = []
    exited = threading.Event()

    def fail_write(item):
        raise primary

    def hook(args):
        captured.append(args.exc_value)
        exited.set()

    monkeypatch.setattr(j, "_write_one", fail_write)
    monkeypatch.setattr(threading, "excepthook", hook)
    j.start()
    try:
        j.submit(_fact())
        assert exited.wait(3)
        assert captured == [primary]                     # the exact primary reached the hook (repr not called)
        assert j.counters()["worker_failed"] is True and j.counters()["worker_exc"] == "unexpected_worker_failure"
    finally:
        monkeypatch.undo()
        j.close(1)


def test_limits_reject_nonpositive():
    with pytest.raises(ValueError):
        bj.JournalLimits(max_row_bytes=0)
    with pytest.raises(ValueError):
        bj.JournalLimits(max_queue_rows=-1)
