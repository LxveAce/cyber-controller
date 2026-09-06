"""Unit tests for the tool-acquisition job registry (src/core/tool_jobs.py).

The registry runs an INJECTED synthetic worker in a thread — no real tool I/O, network, or disk — so the
lifecycle (states, commit boundary, cancellation semantics, launch-failure recovery, memory bounds,
post-terminal guard, owner binding) is tested deterministically with threading.Event gates.
"""
from __future__ import annotations

import threading
import time

import pytest

from src.core import tool_jobs as tj


def _wait_terminal(reg, job_id, owner, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = reg.get(job_id, owner)
        if snap and not snap["active"]:
            return snap
        time.sleep(0.005)
    raise AssertionError(f"job {job_id} did not reach a terminal state in {timeout}s")


def test_happy_path_reports_progress_and_result():
    reg = tj.JobRegistry()

    def worker(emit, should_cancel, begin_commit):
        emit("download", 0, 2, "starting")
        emit("download", 2, 2, "done")
        begin_commit()
        return {"path": "/tools/aircrack-ng/aircrack-ng.exe"}

    jid = reg.start("aircrack-ng", "/tools/aircrack-ng", "sess:1", worker)
    snap = _wait_terminal(reg, jid, "sess:1")
    assert snap["state"] == tj.SUCCEEDED
    assert snap["completed"] == 2 and snap["total"] == 2
    assert "starting" in snap["log"] and "done" in snap["log"]
    assert reg.result(jid, "sess:1") == {"path": "/tools/aircrack-ng/aircrack-ng.exe"}
    assert reg.result(jid, "sess:OTHER") is None          # result is owner-bound


def test_worker_exception_is_a_failed_job_with_error():
    reg = tj.JobRegistry()

    def worker(emit, should_cancel, begin_commit):
        raise RuntimeError("integrity check failed")

    jid = reg.start("aircrack-ng", "/tools/aircrack-ng", "sess:1", worker)
    snap = _wait_terminal(reg, jid, "sess:1")
    assert snap["state"] == tj.FAILED
    assert "integrity check failed" in snap["error"]


# ── J1: commit boundary — a cancel after begin_commit is too late; a committed return stays succeeded ──

def test_cancel_after_commit_stays_succeeded():
    reg = tj.JobRegistry()
    committed, release = threading.Event(), threading.Event()

    def worker(emit, should_cancel, begin_commit):
        begin_commit()          # point of no return
        committed.set()         # "published" the tool
        release.wait(2.0)
        return {"path": "/x"}

    jid = reg.start("aircrack-ng", "/tools/x", "sess:1", worker)
    assert committed.wait(2.0)
    assert reg.cancel(jid, "sess:1") is False   # J1: too late — the job is committing
    release.set()
    snap = _wait_terminal(reg, jid, "sess:1")
    assert snap["state"] == tj.SUCCEEDED         # a committed result is NOT downgraded to cancelled
    assert reg.result(jid, "sess:1") == {"path": "/x"}


def test_cancel_before_commit_is_cancelled():
    reg = tj.JobRegistry()
    started = threading.Event()

    def worker(emit, should_cancel, begin_commit):
        started.set()
        while not should_cancel():
            time.sleep(0.005)
        begin_commit()          # raises JobCancelled because a cancel is pending
        return "ok"

    jid = reg.start("aircrack-ng", "/tools/x", "sess:1", worker)
    assert started.wait(2.0)
    assert reg.cancel(jid, "sess:1") is True
    snap = _wait_terminal(reg, jid, "sess:1")
    assert snap["state"] == tj.CANCELLED
    assert snap["error"] == ""


# ── J2: a cancellation request must not mask an unrelated failure ──

def test_cancel_flag_does_not_mask_unrelated_failure():
    reg = tj.JobRegistry()
    started = threading.Event()

    def worker(emit, should_cancel, begin_commit):
        started.set()
        while not should_cancel():
            time.sleep(0.005)
        raise OSError("disk failure, unrelated to cancel")   # NOT JobCancelled

    jid = reg.start("aircrack-ng", "/tools/x", "sess:1", worker)
    assert started.wait(2.0)
    assert reg.cancel(jid, "sess:1") is True
    snap = _wait_terminal(reg, jid, "sess:1")
    assert snap["state"] == tj.FAILED                 # a real error is a failure, never hidden as cancelled
    assert "disk failure" in snap["error"]


# ── J3: a thread launch failure must release the destination so a retry works ──

def test_thread_launch_failure_releases_destination(monkeypatch):
    reg = tj.JobRegistry()

    def boom(self):
        raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(tj.threading.Thread, "start", boom)
    with pytest.raises(tj.JobLaunchError):
        reg.start("aircrack-ng", "/tools/x", "sess:1", lambda e, s, b: "ok")
    monkeypatch.undo()   # restore real Thread.start for the retry
    jid = reg.start("aircrack-ng", "/tools/x", "sess:1", lambda e, s, b: "ok")
    assert _wait_terminal(reg, jid, "sess:1")["state"] == tj.SUCCEEDED


# ── J4: memory is bounded (per-line cap + terminal-job pruning) ──

def test_log_line_is_capped():
    reg = tj.JobRegistry()

    def worker(emit, should_cancel, begin_commit):
        emit("", None, None, "x" * 5000)   # a huge line
        return "ok"

    jid = reg.start("t", "/tools/x", "sess:1", worker)
    snap = _wait_terminal(reg, jid, "sess:1")
    assert len(snap["log"][0]) <= tj.MAX_LINE_CHARS + 16   # capped (+ the truncation marker)


def test_terminal_jobs_are_pruned():
    reg = tj.JobRegistry(max_terminal=2)
    ids = []
    for i in range(5):
        jid = reg.start("t", f"/tools/x{i}", "sess:1", lambda e, s, b: "ok")
        _wait_terminal(reg, jid, "sess:1")
        ids.append(jid)
    present = [j for j in ids if reg.get(j, "sess:1") is not None]
    assert present == ids[-2:]   # only the two most recent terminal jobs are retained


# ── J5: progress emitted after a terminal transition must be ignored ──

def test_progress_after_terminal_is_ignored():
    reg = tj.JobRegistry()
    late = threading.Event()

    def worker(emit, should_cancel, begin_commit):
        emit("download", 5, 10, "mid")

        def helper():
            late.wait(2.0)
            emit("still downloading", 99, 100, "late line")   # fires AFTER the worker returns

        threading.Thread(target=helper, daemon=True).start()
        return "ok"

    jid = reg.start("t", "/tools/x", "sess:1", worker)
    _wait_terminal(reg, jid, "sess:1")
    late.set()
    time.sleep(0.05)
    snap2 = reg.get(jid, "sess:1")
    assert snap2["state"] == tj.SUCCEEDED
    assert snap2["phase"] == "download" and snap2["completed"] == 5   # not mutated by the late emit
    assert "late line" not in snap2["log"]


def test_new_phase_resets_progress_counters():
    reg = tj.JobRegistry()

    def worker(emit, should_cancel, begin_commit):
        emit("download", 5, 10, None)
        emit("verify", None, None, None)   # a new phase clears the old 5/10 (None alone couldn't)
        return "ok"

    jid = reg.start("t", "/tools/x", "sess:1", worker)
    snap = _wait_terminal(reg, jid, "sess:1")
    assert snap["phase"] == "verify" and snap["completed"] is None and snap["total"] is None


# ── one-writer-per-destination + owner binding ──

def test_one_writer_per_destination_conflicts():
    reg = tj.JobRegistry()
    hold = threading.Event()
    jid = reg.start("aircrack-ng", "/tools/x", "sess:1", lambda e, s, b: hold.wait(2.0))
    try:
        with pytest.raises(tj.JobConflict):
            reg.start("aircrack-ng", "/tools/x", "sess:1", lambda e, s, b: "ok")
    finally:
        hold.set()
    _wait_terminal(reg, jid, "sess:1")
    jid2 = reg.start("aircrack-ng", "/tools/x", "sess:1", lambda e, s, b: "ok")
    assert _wait_terminal(reg, jid2, "sess:1")["state"] == tj.SUCCEEDED   # freed after terminal


def test_owner_binding_blocks_cross_session_read_and_cancel():
    reg = tj.JobRegistry()
    hold = threading.Event()
    jid = reg.start("aircrack-ng", "/tools/x", "sess:1", lambda e, s, b: hold.wait(2.0))
    try:
        assert reg.get(jid, "sess:OTHER") is None
        assert reg.cancel(jid, "sess:OTHER") is None
        assert reg.get(jid, "sess:1") is not None
    finally:
        hold.set()
    _wait_terminal(reg, jid, "sess:1")


def test_cancel_after_terminal_is_too_late():
    reg = tj.JobRegistry()
    jid = reg.start("aircrack-ng", "/tools/x", "sess:1", lambda e, s, b: "ok")
    _wait_terminal(reg, jid, "sess:1")
    assert reg.cancel(jid, "sess:1") is False


# ── J4a: repeated thread-launch failures must be pruned like any other terminal job ──

def test_launch_failures_are_pruned_to_the_terminal_cap(monkeypatch):
    reg = tj.JobRegistry(max_terminal=2)

    def boom(self):
        raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(tj.threading.Thread, "start", boom)
    for i in range(12):
        with pytest.raises(tj.JobLaunchError):
            reg.start("t", f"/tools/x{i}", "sess:1", lambda e, s, b: "ok")
    # J4a: a launch failure is a terminal transition, so terminal pruning bounds it. The J3 fix released
    # the reservation but left the failed record unpruned — 12 failures would otherwise retain 12 records.
    assert len(reg._jobs) == 2
    monkeypatch.undo()
    # the destination of a failed launch is still reusable (J3 behaviour preserved)
    jid = reg.start("t", "/tools/x0", "sess:1", lambda e, s, b: "ok")
    assert _wait_terminal(reg, jid, "sess:1")["state"] == tj.SUCCEEDED


# ── J4b: result is a bounded typed envelope; phase + counters are bounded immutable primitives ──

_INVALID_ENVELOPE = {"result_dropped": "worker result was not a valid tool-result envelope"}


def test_non_dict_result_is_rejected_as_a_non_envelope():
    reg = tj.JobRegistry()
    jid = reg.start("t", "/tools/x", "sess:1", lambda e, s, b: "just a string")
    _wait_terminal(reg, jid, "sess:1")
    assert reg.result(jid, "sess:1") == _INVALID_ENVELOPE     # only None or a flat dict is a valid envelope


def test_nested_container_result_is_rejected():
    reg = tj.JobRegistry()
    jid = reg.start("t", "/tools/x", "sess:1", lambda e, s, b: {"path": "/x", "nested": {"k": 1}})
    _wait_terminal(reg, jid, "sess:1")
    assert reg.result(jid, "sess:1") == _INVALID_ENVELOPE     # a nested dict value is not a bounded primitive


def test_non_serializable_result_is_dropped_not_retained():
    reg = tj.JobRegistry()
    jid = reg.start("t", "/tools/x", "sess:1", lambda e, s, b: {"handle": object()})
    _wait_terminal(reg, jid, "sess:1")
    assert reg.result(jid, "sess:1") == _INVALID_ENVELOPE


def test_over_length_string_value_is_dropped():
    reg = tj.JobRegistry()
    big = "x" * (tj.MAX_RESULT_VALUE_CHARS + 1)
    jid = reg.start("t", "/tools/x", "sess:1", lambda e, s, b: {"path": big})
    _wait_terminal(reg, jid, "sess:1")
    assert reg.result(jid, "sess:1") == _INVALID_ENVELOPE


def test_result_over_byte_cap_is_dropped():
    reg = tj.JobRegistry()

    def worker(emit, should_cancel, begin_commit):
        # each value is within the per-value cap, but the whole envelope serializes past MAX_RESULT_BYTES
        return {f"k{i}": "v" * 2000 for i in range(6)}

    jid = reg.start("t", "/tools/x", "sess:1", worker)
    _wait_terminal(reg, jid, "sess:1")
    assert reg.result(jid, "sess:1") == {
        "result_dropped": f"worker result exceeded {tj.MAX_RESULT_BYTES} bytes"
    }


# J8 — non-finite numbers are not standard JSON; they must be dropped, never retained as `NaN`/`Infinity`
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_result_value_is_dropped(bad):
    reg = tj.JobRegistry()
    jid = reg.start("t", "/tools/x", "sess:1", lambda e, s, b: {"value": bad})
    _wait_terminal(reg, jid, "sess:1")
    assert reg.result(jid, "sess:1") == _INVALID_ENVELOPE


def test_valid_flat_envelope_round_trips_as_an_independent_copy():
    reg = tj.JobRegistry()
    jid = reg.start("t", "/tools/x", "sess:1",
                    lambda e, s, b: {"path": "/tools/x/bin", "version": "1.0", "verified": True})
    _wait_terminal(reg, jid, "sess:1")
    first = reg.result(jid, "sess:1")
    first["path"] = "TAMPERED"                                # mutating the returned copy must not leak
    assert reg.result(jid, "sess:1") == {"path": "/tools/x/bin", "version": "1.0", "verified": True}


# J7 — a result-normalization failure must not strand the destination reservation
def test_result_serializer_fault_keeps_destination_reusable(monkeypatch):
    reg = tj.JobRegistry()
    real_dumps = tj.json.dumps

    def boom(*a, **k):
        raise MemoryError("simulated serializer resource failure")   # no real memory exhaustion

    monkeypatch.setattr(tj.json, "dumps", boom)
    jid = reg.start("t", "/tools/x", "sess:1", lambda e, s, b: {"path": "/tools/x/bin"})
    snap = _wait_terminal(reg, jid, "sess:1")
    assert snap["state"] == tj.SUCCEEDED                  # honest: the underlying operation DID complete
    assert reg.result(jid, "sess:1") == _INVALID_ENVELOPE  # but the un-serializable outcome is a diagnostic
    monkeypatch.setattr(tj.json, "dumps", real_dumps)
    # the reservation was released despite the normalization fault — a same-destination job can start again
    jid2 = reg.start("t", "/tools/x", "sess:1", lambda e, s, b: {"ok": True})
    assert _wait_terminal(reg, jid2, "sess:1")["state"] == tj.SUCCEEDED


# J4b — progress counters accept only bounded immutable ints (or None); everything else is rejected
def test_progress_counters_reject_mutable_and_invalid_values():
    reg = tj.JobRegistry()
    big_dict = {"x": "y" * 2_097_152}

    def worker(emit, should_cancel, begin_commit):
        emit("download", big_dict, ["a"] * 100000, None)   # a dict + a list can't be retained as counters
        emit("download", True, -5, None)                    # a bool and a negative are not valid counters
        return {"ok": True}

    jid = reg.start("t", "/tools/x", "sess:1", worker)
    snap = _wait_terminal(reg, jid, "sess:1")
    assert snap["completed"] is None and snap["total"] is None


def test_over_bound_and_subclass_counters_are_rejected():
    reg = tj.JobRegistry()

    class EvilInt(int):
        pass

    def worker(emit, should_cancel, begin_commit):
        emit("download", 1 << 16384, None, None)   # huge int: serializes past json's digit limit -> reject
        big = EvilInt(3)
        setattr(big, "payload", {"x": "y" * 2_097_152})  # an int subclass smuggling a mutable attr -> reject
        emit("download", big, None, None)
        emit("download", 2 ** 53, None, None)       # just past the JS-safe ceiling -> reject
        return {"ok": True}

    jid = reg.start("t", "/tools/x", "sess:1", worker)
    snap = _wait_terminal(reg, jid, "sess:1")
    assert snap["completed"] is None               # none of the three over-bound/subclass values were stored


def test_str_subclass_phase_is_rejected():
    reg = tj.JobRegistry()

    class EvilStr(str):
        pass

    def worker(emit, should_cancel, begin_commit):
        p = EvilStr("download")
        setattr(p, "payload", {"x": "y" * 2_097_152})   # short label, but a mutable attribute rides along
        emit(p, 1, 2, None)
        return {"ok": True}

    jid = reg.start("t", "/tools/x", "sess:1", worker)
    snap = _wait_terminal(reg, jid, "sess:1")
    assert snap["phase"] == ""                      # the str subclass was rejected, not stored by identity


def test_valid_counters_store_and_snapshot_is_isolated():
    reg = tj.JobRegistry()

    def worker(emit, should_cancel, begin_commit):
        emit("download", 3, 10, None)
        return {"ok": True}

    jid = reg.start("t", "/tools/x", "sess:1", worker)
    snap = _wait_terminal(reg, jid, "sess:1")
    assert snap["completed"] == 3 and snap["total"] == 10
    snap["completed"] = 999                                  # mutating the snapshot must not change the store
    assert reg.get(jid, "sess:1")["completed"] == 3


# J4b — a phase is rejected (never truncated) when over-length, so two long names can't alias to one identity
def test_overlong_phases_sharing_a_prefix_do_not_alias():
    reg = tj.JobRegistry()
    prefix = "p" * tj.MAX_PHASE_CHARS

    def worker(emit, should_cancel, begin_commit):
        emit(prefix + "A", 5, 10, None)         # over-length -> phase AND its counters rejected
        emit(prefix + "B", None, None, None)    # a DIFFERENT over-length name -> also rejected, no aliasing
        return {"ok": True}

    jid = reg.start("t", "/tools/x", "sess:1", worker)
    snap = _wait_terminal(reg, jid, "sess:1")
    assert snap["phase"] == ""                              # neither long name became the phase
    assert not snap["phase"].startswith(prefix)


def test_valid_new_phase_resets_counters_but_rejected_phase_does_not_change_them():
    reg = tj.JobRegistry()

    def worker(emit, should_cancel, begin_commit):
        emit("download", 5, 10, None)
        emit("p" * 5000, None, None, None)      # a rejected phase: phase and counters stay as they were
        return {"ok": True}

    jid = reg.start("t", "/tools/x", "sess:1", worker)
    snap = _wait_terminal(reg, jid, "sess:1")
    assert snap["phase"] == "download" and snap["completed"] == 5 and snap["total"] == 10


def test_rejected_nonempty_phase_discards_its_counters():
    reg = tj.JobRegistry()

    def worker(emit, should_cancel, begin_commit):
        emit("download", 5, 10, None)
        emit("p" * (tj.MAX_PHASE_CHARS + 1), 99, 100, None)   # invalid phase -> its 99/100 must NOT be applied
        return {"ok": True}

    jid = reg.start("t", "/tools/x", "sess:1", worker)
    snap = _wait_terminal(reg, jid, "sess:1")
    assert snap["phase"] == "download" and snap["completed"] == 5 and snap["total"] == 10


def test_empty_phase_sentinel_still_applies_counters():
    reg = tj.JobRegistry()

    def worker(emit, should_cancel, begin_commit):
        emit("download", 1, 10, None)
        emit("", 5, 10, None)          # empty phase = documented no-phase-change; counters still update
        return {"ok": True}

    jid = reg.start("t", "/tools/x", "sess:1", worker)
    snap = _wait_terminal(reg, jid, "sess:1")
    assert snap["phase"] == "download" and snap["completed"] == 5 and snap["total"] == 10


# J4b — result field count is bounded BEFORE any copy/serialize; JS-unsafe ints and subtypes are rejected
def test_result_with_too_many_fields_is_rejected_without_serializing(monkeypatch):
    reg = tj.JobRegistry()
    # A COUNTING spy that delegates to the real serializer (not a raising mock): production catches serializer
    # exceptions, so a raising mock couldn't prove the serializer was skipped — an empty call list can.
    calls = []
    real_dumps = tj.json.dumps

    def counting_dumps(*a, **k):
        calls.append(1)
        return real_dumps(*a, **k)

    monkeypatch.setattr(tj.json, "dumps", counting_dumps)
    over = {f"k{i}": i for i in range(tj.MAX_RESULT_FIELDS + 1)}
    jid = reg.start("t", "/tools/x", "sess:1", lambda e, s, b: over)
    _wait_terminal(reg, jid, "sess:1")
    assert reg.result(jid, "sess:1") == _INVALID_ENVELOPE   # rejected on field count...
    assert calls == []                                       # ...BEFORE the serializer was ever called


def test_result_js_unsafe_int_is_rejected():
    reg = tj.JobRegistry()
    jid = reg.start("t", "/tools/x", "sess:1", lambda e, s, b: {"n": 2 ** 53 + 1})
    _wait_terminal(reg, jid, "sess:1")
    assert reg.result(jid, "sess:1") == _INVALID_ENVELOPE


def test_result_dict_subclass_is_rejected():
    reg = tj.JobRegistry()

    class EvilDict(dict):
        pass

    jid = reg.start("t", "/tools/x", "sess:1", lambda e, s, b: EvilDict(path="/x"))
    _wait_terminal(reg, jid, "sess:1")
    assert reg.result(jid, "sess:1") == _INVALID_ENVELOPE   # exact-type contract: a dict subclass is not a dict


# Inherited failure-path strand: an exception whose __str__ raises must still release the destination
def test_unrenderable_worker_error_does_not_strand_destination():
    reg = tj.JobRegistry()

    class NastyError(RuntimeError):
        def __str__(self):
            raise ValueError("this __str__ raises")

    def worker(emit, should_cancel, begin_commit):
        raise NastyError()

    jid = reg.start("t", "/tools/x", "sess:1", worker)
    snap = _wait_terminal(reg, jid, "sess:1")
    assert snap["state"] == tj.FAILED and snap["error"]     # a bounded fallback message, no crash
    # the destination was released despite the un-renderable error
    jid2 = reg.start("t", "/tools/x", "sess:1", lambda e, s, b: {"ok": True})
    assert _wait_terminal(reg, jid2, "sess:1")["state"] == tj.SUCCEEDED


# Residual: the phase sentinel check must not consult a foreign object's __eq__ before the exact-type gate
def test_empty_str_subclass_phase_is_rejected_not_treated_as_sentinel():
    reg = tj.JobRegistry()

    class Emptyish(str):
        pass

    def worker(emit, should_cancel, begin_commit):
        emit("download", 5, 10, None)
        emit(Emptyish(""), 99, 100, None)   # equals "" but not an exact str -> malformed -> counters dropped
        return {"ok": True}

    jid = reg.start("t", "/tools/x", "sess:1", worker)
    snap = _wait_terminal(reg, jid, "sess:1")
    assert snap["phase"] == "download" and snap["completed"] == 5 and snap["total"] == 10


def test_phase_object_with_raising_eq_is_rejected_without_failing_the_job():
    reg = tj.JobRegistry()

    class RaisingEq:
        def __eq__(self, other):
            raise ValueError("__eq__ must never be consulted for a malformed phase")

    def worker(emit, should_cancel, begin_commit):
        emit("download", 5, 10, None)
        emit(RaisingEq(), 99, 100, None)    # exact-type gate rejects it BEFORE any == is evaluated
        return {"ok": True}

    jid = reg.start("t", "/tools/x", "sess:1", worker)
    snap = _wait_terminal(reg, jid, "sess:1")
    assert snap["state"] == tj.SUCCEEDED     # a malformed phase is ignored, never a worker failure
    assert snap["phase"] == "download" and snap["completed"] == 5 and snap["total"] == 10


# Residual: error formatting must stay inside the guard (a str subclass can re-enter a raising __str__)
def test_error_str_subclass_with_raising_str_does_not_strand():
    reg = tj.JobRegistry()

    class BadStr(str):
        def __str__(self):
            raise ValueError("second conversion raises")

    class NastyError(RuntimeError):
        def __str__(self):
            return BadStr("x")               # str(exc) succeeds, but the returned subclass re-raises later

    def worker(emit, should_cancel, begin_commit):
        raise NastyError()

    jid = reg.start("t", "/tools/x", "sess:1", worker)
    snap = _wait_terminal(reg, jid, "sess:1")
    assert snap["state"] == tj.FAILED and snap["error"]
    jid2 = reg.start("t", "/tools/x", "sess:1", lambda e, s, b: {"ok": True})
    assert _wait_terminal(reg, jid2, "sess:1")["state"] == tj.SUCCEEDED


def test_unprintable_launch_error_raises_joblauncherror(monkeypatch):
    reg = tj.JobRegistry()

    class UnprintableLaunch(RuntimeError):
        def __str__(self):
            raise ValueError("cannot render this launch error")

    def boom(self):
        raise UnprintableLaunch()

    monkeypatch.setattr(tj.threading.Thread, "start", boom)
    with pytest.raises(tj.JobLaunchError):          # NOT a raw ValueError from re-rendering str(exc)
        reg.start("t", "/tools/x", "sess:1", lambda e, s, b: {"ok": True})
    monkeypatch.undo()
    jid = reg.start("t", "/tools/x", "sess:1", lambda e, s, b: {"ok": True})
    assert _wait_terminal(reg, jid, "sess:1")["state"] == tj.SUCCEEDED   # destination released


@pytest.mark.parametrize("kwargs", [
    {"log_lines": None}, {"log_lines": 0}, {"log_lines": -1}, {"log_lines": True},
    {"max_terminal": None}, {"max_terminal": 0}, {"max_terminal": -5}, {"max_terminal": False},
])
def test_invalid_limits_are_rejected_at_construction(kwargs):
    with pytest.raises(ValueError):
        tj.JobRegistry(**kwargs)


# ── J6: retention is by completion order — a long job that finishes last must survive ──

def test_freshly_completed_long_job_is_retained_over_older_short_jobs():
    reg = tj.JobRegistry(max_terminal=2)
    hold = threading.Event()

    def slow_worker(emit, should_cancel, begin_commit):
        hold.wait(3.0)
        return {"status": "slow-done"}

    slow = reg.start("slow", "/tools/slow", "sess:1", slow_worker)

    short_ids = []
    for i in range(3):
        jid = reg.start("t", f"/tools/short{i}", "sess:1", lambda e, s, b: "ok")
        _wait_terminal(reg, jid, "sess:1")
        short_ids.append(jid)

    assert reg.get(slow, "sess:1")["active"] is True     # long job still running while 3 newer jobs finished
    hold.set()
    slow_snap = _wait_terminal(reg, slow, "sess:1")

    # J6: the long job completed LAST, so it must be retained (its outcome is available for reconnect)...
    assert slow_snap["state"] == tj.SUCCEEDED
    assert reg.result(slow, "sess:1") == {"status": "slow-done"}
    # ...and an OLDER-completed short job is pruned in its place; the newest short job stays.
    assert reg.get(short_ids[1], "sess:1") is None
    assert reg.get(short_ids[2], "sess:1") is not None


# ── status_and_result: atomic owner-scoped status+result (route retention-race helper) ──

def test_status_and_result_atomic_for_owned_success():
    reg = tj.JobRegistry()
    jid = reg.start("t", "/tools/x", "sess:1", lambda e, s, b: {"path": "/x/bin"})
    _wait_terminal(reg, jid, "sess:1")
    bundle = reg.status_and_result(jid, "sess:1")
    assert bundle is not None
    assert bundle["is_success"] is True
    assert bundle["snapshot"]["state"] == tj.SUCCEEDED
    assert bundle["result"] == {"path": "/x/bin"}
    assert reg.status_and_result(jid, "sess:OTHER") is None   # foreign owner -> None (route maps to 404)
    assert reg.status_and_result("no-such-job", "sess:1") is None


def test_status_and_result_non_success_has_no_result():
    reg = tj.JobRegistry()

    def worker(emit, should_cancel, begin_commit):
        raise RuntimeError("boom")

    jid = reg.start("t", "/tools/x", "sess:1", worker)
    _wait_terminal(reg, jid, "sess:1")
    bundle = reg.status_and_result(jid, "sess:1")
    assert bundle["is_success"] is False and bundle["result"] is None
    assert bundle["snapshot"]["state"] == tj.FAILED


# ── on_finish: a per-job finalizer runs EXACTLY once on every terminal path (queue-lifetime cleanup) ──

def test_on_finish_runs_once_on_success():
    reg = tj.JobRegistry()
    calls = []
    jid = reg.start("t", "/tools/x", "s", lambda e, s, b: {"ok": True}, on_finish=lambda: calls.append(1))
    _wait_terminal(reg, jid, "s")
    time.sleep(0.02)
    assert calls == [1]


def test_on_finish_runs_once_on_failure():
    reg = tj.JobRegistry()
    calls = []

    def worker(emit, should_cancel, begin_commit):
        raise RuntimeError("boom")

    jid = reg.start("t", "/tools/x", "s", worker, on_finish=lambda: calls.append(1))
    _wait_terminal(reg, jid, "s")
    time.sleep(0.02)
    assert calls == [1]


def test_on_finish_runs_on_explicit_cancel():
    reg = tj.JobRegistry()
    calls = []
    started = threading.Event()

    def worker(emit, should_cancel, begin_commit):
        started.set()
        while not should_cancel():
            time.sleep(0.005)
        begin_commit()          # raises JobCancelled (cancel is pending)
        return {"ok": True}

    jid = reg.start("t", "/tools/x", "s", worker, on_finish=lambda: calls.append(1))
    assert started.wait(2.0)
    reg.cancel(jid, "s")
    assert _wait_terminal(reg, jid, "s")["state"] == tj.CANCELLED
    time.sleep(0.02)
    assert calls == [1]


def test_on_finish_runs_on_launch_failure(monkeypatch):
    reg = tj.JobRegistry()
    calls = []

    def boom(self):
        raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(tj.threading.Thread, "start", boom)
    with pytest.raises(tj.JobLaunchError):
        reg.start("t", "/tools/x", "s", lambda e, s, b: {"ok": True}, on_finish=lambda: calls.append(1))
    assert calls == [1]                         # a launch failure still runs the finalizer


def test_on_finish_runs_on_cancel_before_the_worker_runs(monkeypatch):
    reg = tj.JobRegistry()
    calls = []
    monkeypatch.setattr(tj.threading.Thread, "start", lambda self: None)   # don't auto-run the worker
    jid = reg.start("t", "/tools/x", "s", lambda e, s, b: {"ok": True}, on_finish=lambda: calls.append(1))
    assert reg.cancel(jid, "s") is True         # cancel lands before the worker executes
    monkeypatch.undo()
    reg._run(jid, lambda e, s, b: {"ok": True})  # now drive the run: sees _cancel -> CANCELLED path
    assert reg.get(jid, "s")["state"] == tj.CANCELLED
    assert calls == [1]


def test_cleanup_failure_is_visible_without_relabelling_success():
    reg = tj.JobRegistry()

    def bad_finish():
        raise OSError("release failed and the destination is still reserved")

    jid = reg.start("t", "/tools/x", "s", lambda e, s, b: {"path": "/x"}, on_finish=bad_finish)
    snap = _wait_terminal(reg, jid, "s")
    assert snap["state"] == tj.SUCCEEDED and snap["error"] == ""     # committed work is NOT relabelled
    assert reg.result(jid, "s") == {"path": "/x"}                    # the result is preserved
    assert any("cleanup" in line for line in snap["log"])           # F1: an owner-visible note appears...
    assert not any("release failed" in line for line in snap["log"])  # ...without the callback's own text


def test_terminal_job_clears_the_spent_finalizer_reference():
    reg = tj.JobRegistry()
    jid = reg.start("t", "/tools/x", "s", lambda e, s, b: {"ok": True}, on_finish=lambda: None)
    _wait_terminal(reg, jid, "s")
    time.sleep(0.02)
    assert reg._jobs[jid].on_finish is None   # F2: the spent callback is dropped after its single invocation
