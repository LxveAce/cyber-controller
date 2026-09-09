"""Tests for the bounded, memory-only AntiHunter incidents.jsonl reader. Synthetic bytes authored here; no real
logs, no files on disk, no upstream code. Proves the accepted six-field schema is enforced BEFORE category
classification (missing/extra/non-string/invalid-clock -> malformed with a fixed reason, never admitted with a
nulled clock and never mislabelled unsupported), plus identity, line handling, duplicate preservation, limits,
and that no raw/node/src is retained in the model.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from src.core.incident_import import (
    MAX_FILE_BYTES,
    MAX_LINE_BYTES,
    MAX_LINES,
    IncidentEvent,
    import_incidents,
)


def _line(**fields) -> bytes:
    return (json.dumps(fields) + "\n").encode("utf-8")


def _supported(**over) -> bytes:
    row = {"ts": 1000, "epoch": 1_700_000_000, "node": "!aabbcc", "src": "esp32", "type": "EVILTWIN",
           "raw": "ssid=Free_Wifi bssid=AA:BB:CC:DD:EE:FF"}
    row.update(over)
    return _line(**row)


# ── admitted well-formed rows keep only label/position/clock; no raw/node/src ──────────────────────

def test_supported_row_is_admitted_with_only_label_position_and_clock():
    rep = import_incidents(_supported())
    assert rep.counts["admitted"] == 1
    ev = rep.events[0]
    assert ev == IncidentEvent(line=1, category="EVILTWIN", ts=1000, epoch=1_700_000_000)
    # the event dataclass has no field for node/src/raw at all
    assert not hasattr(ev, "raw") and not hasattr(ev, "node") and not hasattr(ev, "src")


def test_all_seven_supported_labels_admit():
    for t in ("EVILTWIN", "BEACON_FORGE", "DEAUTH_FORGE", "DEAUTH_FLOOD", "PROBE_FLOOD", "CSA_SPOOF",
              "SSID_CONFUSION"):
        rep = import_incidents(_supported(type=t))
        assert rep.events and rep.events[0].category == t


# ── IC-1: the accepted six-field schema is enforced BEFORE classification (root's twelve witnesses) ──

@pytest.mark.parametrize("missing", ["ts", "epoch", "node", "src", "type", "raw"])
def test_missing_any_required_field_is_malformed_not_admitted(missing):
    row = {"ts": 1000, "epoch": 1_700_000_000, "node": "!aabbcc", "src": "esp32", "type": "EVILTWIN",
           "raw": "r"}
    del row[missing]
    rep = import_incidents(_line(**row))
    assert rep.counts["admitted"] == 0 and rep.counts["malformed"] == 1
    assert rep.malformed == [{"line": 1, "reason": "invalid-field-set"}]


def test_extra_field_is_malformed_not_admitted():
    rep = import_incidents(_supported(extra="nope"))     # a seventh field
    assert rep.counts["admitted"] == 0 and rep.counts["malformed"] == 1
    assert rep.malformed == [{"line": 1, "reason": "invalid-field-set"}]


@pytest.mark.parametrize("bad", [[1, 2], {"x": 1}, 5, None, True])
@pytest.mark.parametrize("field_name", ["node", "src", "type", "raw"])
def test_nonstring_text_field_is_malformed(field_name, bad):
    rep = import_incidents(_supported(**{field_name: bad}))
    assert rep.counts["admitted"] == 0 and rep.counts["malformed"] == 1
    assert rep.malformed == [{"line": 1, "reason": "invalid-string-field"}]


@pytest.mark.parametrize("bad", [True, False, 1.5, float("inf"), "1000", -1, 2 ** 32, 2 ** 40, [1], {"x": 1}])
@pytest.mark.parametrize("field_name", ["ts", "epoch"])
def test_invalid_clock_field_is_malformed_not_admitted_with_none(field_name, bad):
    rep = import_incidents(_supported(**{field_name: bad}))
    assert rep.counts["admitted"] == 0 and rep.counts["malformed"] == 1
    assert rep.malformed == [{"line": 1, "reason": "invalid-clock-field"}]


def test_unknown_category_that_also_fails_schema_is_malformed_not_unsupported():
    # root witness: an unsupported type with the other five fields missing -> schema fails FIRST -> malformed.
    rep = import_incidents(_line(type="SOME_FUTURE_TYPE"))
    assert rep.counts["malformed"] == 1 and rep.counts["unsupported"] == 0
    assert rep.malformed == [{"line": 1, "reason": "invalid-field-set"}]


def test_well_formed_record_with_unknown_category_is_unsupported_not_malformed():
    rep = import_incidents(_supported(type="SOME_FUTURE_TYPE"))
    assert rep.counts["unsupported"] == 1 and rep.counts["malformed"] == 0
    assert rep.unsupported == [{"line": 1, "reason": "unsupported-type"}]
    # the unknown type string is never stored
    assert "SOME_FUTURE_TYPE" not in json.dumps(rep.unsupported)


def test_admitted_record_never_carries_a_nulled_clock():
    # every admitted event has both validated integer clocks; a bad clock makes the whole row malformed
    rep = import_incidents(_supported())
    assert all(isinstance(e.ts, int) and isinstance(e.epoch, int) for e in rep.events)


# ── non-object / bad JSON ──────────────────────────────────────────────────────────────────────────

def test_non_object_and_bad_json_are_malformed():
    rep = import_incidents(b'[1,2,3]\nnot json at all\n42\n')
    assert rep.counts["malformed"] == 3
    reasons = {m["reason"] for m in rep.malformed}
    assert reasons == {"not-an-object", "invalid-json"}


# ── separate counts; blanks; no all-clear ────────────────────────────────────────────────────────────

def test_counts_are_separate_and_a_partial_input_is_no_all_clear():
    data = _supported() + b"\n" + b"   \n" + _supported(type="UNKNOWN") + b"garbage\n"
    rep = import_incidents(data)
    assert rep.counts["admitted"] == 1 and rep.counts["blank"] == 2   # an empty line and a whitespace-only line
    assert rep.counts["unsupported"] == 1 and rep.counts["malformed"] == 1
    assert rep.counts["total_lines"] == 5
    # sum of the disjoint categories equals the total physical lines counted
    assert sum(rep.counts[k] for k in ("admitted", "blank", "unsupported", "malformed")) == rep.counts["total_lines"]


# ── limits validated before parse ────────────────────────────────────────────────────────────────────

def test_oversized_file_is_recorded_not_parsed():
    data = b"x" * (MAX_FILE_BYTES + 1)
    rep = import_incidents(data)
    assert rep.truncated and rep.truncation_reason and "2MiB" in rep.truncation_reason
    assert rep.events == [] and rep.counts["total_lines"] == 0
    assert rep.file_sha256 == hashlib.sha256(data).hexdigest()   # identity still recorded


def test_too_many_lines_truncates_to_the_cap():
    data = _supported() * (MAX_LINES + 5)
    rep = import_incidents(data)
    assert rep.truncated and str(MAX_LINES) in rep.truncation_reason
    assert rep.counts["total_lines"] == MAX_LINES


def test_oversized_line_is_malformed_never_parsed():
    big = _line(ts=1, epoch=1, node="n", src="s", type="EVILTWIN", raw="A" * (MAX_LINE_BYTES + 10))
    rep = import_incidents(big)
    assert rep.counts["malformed"] == 1 and rep.malformed[0]["reason"] == "line-exceeds-16KiB"


# ── never repair / join; incomplete final; duplicates preserved ──────────────────────────────────────

def test_broken_control_char_line_is_not_joined_into_an_event():
    # A raw newline inside a value splits it into two physical lines; each fails JSON, and they are NEVER joined.
    broken = b'{"ts":1,"epoch":1,"node":"n","src":"s","type":"EVILTWIN","raw":"line1\nline2"}\n'
    rep = import_incidents(broken)
    assert rep.counts["admitted"] == 0 and rep.counts["malformed"] == 2   # two broken physical lines


def test_valid_final_object_without_trailing_newline_is_admitted_and_flagged():
    data = _supported().rstrip(b"\n")   # no trailing newline
    rep = import_incidents(data)
    assert rep.counts["admitted"] == 1 and rep.final_line_without_newline is True


def test_truncated_final_record_is_malformed():
    data = _supported() + b'{"ts":1,"epoch":1,"type":"EVILT'   # cut off mid-object, no newline
    rep = import_incidents(data)
    assert rep.counts["admitted"] == 1 and rep.counts["malformed"] == 1


def test_duplicate_records_are_not_collapsed_and_keep_distinct_line_identity():
    data = _supported() + _supported()   # two byte-identical rows
    rep = import_incidents(data)
    assert rep.counts["admitted"] == 2
    assert [e.line for e in rep.events] == [1, 2]        # physical line position = distinct event identity


def test_file_sha256_is_report_identity():
    data = _supported()
    assert import_incidents(data).file_sha256 == hashlib.sha256(data).hexdigest()
