"""Standalone tests for the offline workspace search & filter core (workspace_search.py).

Run directly: ``python test_workspace_search.py``. It loads the module UNDER TEST by FILE PATH via importlib
(no src.core package import, no pytest, no conftest, no app), exercises real assertions, and exits nonzero on
the first failure. All records here are synthetic dicts authored in this file — no real logs, no file reads,
no network. The assertions prove the NEW behaviour (plain-text substring search) plus the safe-field/redaction
boundary, the category/status filters, AND-combination, bounds, and deterministic ordering.
"""
from __future__ import annotations

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.normpath(os.path.join(_HERE, "..", "src", "core", "workspace_search.py"))

_spec = importlib.util.spec_from_file_location("workspace_search_under_test", _MOD_PATH)
if _spec is None or _spec.loader is None:  # explicit check of spec AND its loader before use
    raise ImportError(f"cannot load module under test from {_MOD_PATH}")
ws = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws)

_ASSERTIONS = 0


def eq(actual, expected, label):
    """A real equality assertion that also tallies the assertion count."""
    global _ASSERTIONS
    _ASSERTIONS += 1
    assert actual == expected, f"{label}: {actual!r} != {expected!r}"


def ok(cond, label):
    """A real boolean assertion that also tallies the assertion count."""
    global _ASSERTIONS
    _ASSERTIONS += 1
    assert cond, f"{label}: expected truthy"


def raises(exc_type, fn, label, *, code=None):
    """Assert that *fn* raises *exc_type* (and optionally a fixed .code). Counts as one assertion."""
    global _ASSERTIONS
    _ASSERTIONS += 1
    try:
        fn()
    except exc_type as e:
        if code is not None:
            assert getattr(e, "code", None) == code, f"{label}: code {getattr(e, 'code', None)!r} != {code!r}"
        return
    except Exception as e:  # wrong type
        raise AssertionError(f"{label}: raised {type(e).__name__}, expected {exc_type.__name__}")
    raise AssertionError(f"{label}: did not raise {exc_type.__name__}")


# ── synthetic corpus (already-redacted rows, as export_projection would yield) ──────────────────────────
def corpus():
    return [
        {"line": 1, "status": "admitted", "category": "EVILTWIN", "ts": 100, "epoch": 1700000000, "reason": ""},
        {"line": 2, "status": "admitted", "category": "DEAUTH_FLOOD", "ts": 200, "epoch": 1700000001, "reason": ""},
        {"line": 3, "status": "malformed", "category": "", "ts": None, "epoch": None, "reason": "invalid-json"},
        {"line": 4, "status": "unsupported", "category": "", "ts": None, "epoch": None, "reason": "unsupported-type"},
        {"line": 5, "status": "malformed", "category": "", "ts": None, "epoch": None, "reason": "line-exceeds-16KiB"},
        {"line": 6, "status": "admitted", "category": "PROBE_FLOOD", "ts": 300, "epoch": 1700000002, "reason": ""},
    ]


def lines(result):
    return [r["line"] for r in result["matched"]]


# ── the NEW dimension: plain-text substring search (no existing filter can express this) ────────────────
def test_plain_text_finds_by_reason_substring():
    res = ws.search_records(corpus(), text="json")
    eq(lines(res), [3], "text 'json' matches only the invalid-json reason")
    eq(res["match_count"], 1, "match_count reflects the single hit")
    eq(res["total_records"], 6, "total_records is the full supplied count")
    eq(res["query"]["text"], "json", "query echoes the trimmed text")


def test_plain_text_is_case_insensitive_and_matches_category_label():
    res = ws.search_records(corpus(), text="flood")
    eq(lines(res), [2, 6], "lowercase 'flood' matches DEAUTH_FLOOD and PROBE_FLOOD labels")


def test_plain_text_matches_status_word():
    res = ws.search_records(corpus(), text="MALFORMED")
    eq(lines(res), [3, 5], "uppercase query matches the malformed status field, case-insensitively")


def test_plain_text_no_match_returns_empty_not_error():
    res = ws.search_records(corpus(), text="nonexistent-token")
    eq(res["matched"], [], "a miss is an empty list, not an error or an all-clear")
    eq(res["match_count"], 0, "zero matches")


def test_blank_or_whitespace_text_is_no_filter():
    eq(len(ws.search_records(corpus(), text="   ")["matched"]), 6, "whitespace-only text filters nothing")
    eq(ws.search_records(corpus(), text="   ")["query"]["text"], None, "blank text echoes as no filter")


# ── type/label + status filters, and AND-combination ────────────────────────────────────────────────────
def test_category_filter_exact_label():
    res = ws.search_records(corpus(), category="EVILTWIN")
    eq(lines(res), [1], "category filter narrows to the one EVILTWIN row")


def test_status_filter():
    res = ws.search_records(corpus(), status="admitted")
    eq(lines(res), [1, 2, 6], "status filter returns the three admitted rows in position order")


def test_text_and_category_combine_with_and():
    # 'flood' alone matches lines 2 and 6; adding category pins it to the DEAUTH_FLOOD row only.
    res = ws.search_records(corpus(), text="flood", category="DEAUTH_FLOOD")
    eq(lines(res), [2], "text AND category both applied")


def test_text_and_status_combine_with_and():
    # 'e' appears in many fields; restricting to unsupported keeps only line 4.
    res = ws.search_records(corpus(), text="unsupported", status="unsupported")
    eq(lines(res), [4], "text AND status both applied")


# ── redaction / safe-field boundary: extra keys never match and never leak ──────────────────────────────
def test_extra_keys_are_dropped_never_matched_never_returned():
    smuggled = [{"line": 9, "status": "admitted", "category": "EVILTWIN", "ts": 1, "epoch": 2, "reason": "",
                 "raw": "ssid=SECRET password=hunter2", "node": "!deadbeef", "src": "esp32"}]
    # searching the smuggled raw/node/src text must NOT hit — those fields are not searchable
    eq(ws.search_records(smuggled, text="hunter2")["matched"], [], "raw field is never searched")
    eq(ws.search_records(smuggled, text="deadbeef")["matched"], [], "node field is never searched")
    got = ws.search_records(smuggled, category="EVILTWIN")["matched"][0]
    eq(set(got.keys()), {"line", "status", "category", "ts", "epoch", "reason"}, "only safe fields returned")
    ok("raw" not in got and "node" not in got and "src" not in got, "smuggled keys are dropped from output")


def test_non_string_text_field_cannot_be_smuggled_into_scan():
    # a non-string category becomes "" in the safe projection, so it is not matched as text and not returned
    weird = [{"line": 1, "status": "admitted", "category": 12345, "ts": 1, "epoch": 2, "reason": ""}]
    eq(ws.search_records(weird, text="12345")["matched"], [], "a non-string field is neutralized, not searched")
    eq(ws.search_records(weird, category="EVILTWIN")["matched"], [], "the coerced empty category matches nothing")


# ── deterministic ordering ──────────────────────────────────────────────────────────────────────────────
def test_results_are_sorted_by_position_deterministically():
    shuffled = list(reversed(corpus()))
    res = ws.search_records(shuffled, text="")  # no filter -> all rows, re-sorted
    eq(lines(res), [1, 2, 3, 4, 5, 6], "output is position-ordered regardless of input order")


def test_position_less_rows_sort_last_without_typeerror():
    recs = [{"line": None, "status": "malformed", "category": "", "ts": None, "epoch": None, "reason": "x"},
            {"line": 2, "status": "malformed", "category": "", "ts": None, "epoch": None, "reason": "y"}]
    res = ws.search_records(recs, status="malformed")
    eq([r["line"] for r in res["matched"]], [2, None], "real positions first, position-less last, no crash")


# ── bounds & allowlist errors (fixed codes, no value interpolation) ─────────────────────────────────────
def test_unknown_category_is_fixed_error():
    raises(ws.SearchError, lambda: ws.search_records(corpus(), category="NOT_A_LABEL"),
           "unknown category rejected", code="unknown-category")


def test_unknown_status_is_fixed_error():
    raises(ws.SearchError, lambda: ws.search_records(corpus(), status="bogus"),
           "unknown status rejected", code="unknown-status")


def test_all_sentinel_means_no_filter():
    eq(len(ws.search_records(corpus(), category="all", status="all")["matched"]), 6, "'all' disables filters")


def test_query_too_long_is_fixed_error_before_scan():
    raises(ws.SearchError, lambda: ws.search_records(corpus(), text="x" * (ws.MAX_QUERY_CHARS + 1)),
           "over-long query rejected", code="query-too-long")


def test_too_many_records_is_fixed_error():
    big = [{"line": i, "status": "admitted", "category": "EVILTWIN", "ts": 0, "epoch": 0, "reason": ""}
           for i in range(ws.MAX_RECORDS + 1)]
    raises(ws.SearchError, lambda: ws.search_records(big), "over-cap record set rejected", code="too-many-records")


def test_records_wrong_shape_is_typeerror():
    raises(TypeError, lambda: ws.search_records("not-a-list"), "string is not a record sequence")
    raises(TypeError, lambda: ws.search_records({"a": 1}), "mapping is not a record sequence")


def test_non_string_query_is_typeerror():
    raises(TypeError, lambda: ws.search_records(corpus(), text=123), "numeric text query rejected")


# ── the projection adaptor feeds search exactly what export_projection yields ────────────────────────────
def test_records_from_projection_builds_unified_rows():
    projection = {
        "events": [{"line": 1, "category": "EVILTWIN", "ts": 10, "epoch": 20}],
        "unsupported": [{"line": 2, "reason": "unsupported-type"}],
        "malformed": [{"line": 3, "reason": "invalid-json"}],
    }
    rows = ws.records_from_projection(projection)
    eq(len(rows), 3, "one row per source entry")
    eq(rows[0]["status"], "admitted", "event becomes admitted row")
    eq(rows[0]["category"], "EVILTWIN", "event keeps its label")
    eq(rows[1]["status"], "unsupported", "unsupported entry becomes unsupported row")
    eq(rows[2]["reason"], "invalid-json", "malformed entry keeps its fixed reason")
    # end-to-end: projection -> rows -> search finds the malformed row by text
    res = ws.search_records(ws.records_from_projection(projection), text="json")
    eq(lines(res), [3], "projection feeds search; text finds the malformed row")


def test_records_from_projection_rejects_non_mapping():
    raises(ws.SearchError, lambda: ws.records_from_projection([1, 2, 3]),
           "non-mapping projection rejected", code="projection-not-a-mapping")


def test_records_from_projection_tolerates_missing_groups():
    eq(len(ws.records_from_projection({})), 0, "empty projection yields no rows")


# ── negative controls: malformed projection input never invents rows (finding #4) ───────────────────────
def test_projection_scalar_group_is_fixed_error_not_char_rows():
    # a string group must NOT be iterated character-by-character into invented admitted rows
    raises(ws.SearchError, lambda: ws.records_from_projection({"events": "bad"}),
           "string events group rejected", code="malformed-projection-group")
    # any non-list/tuple group (scalar, mapping) is the same fixed shape error
    raises(ws.SearchError, lambda: ws.records_from_projection({"unsupported": 7}),
           "scalar unsupported group rejected", code="malformed-projection-group")
    raises(ws.SearchError, lambda: ws.records_from_projection({"malformed": {"line": 1}}),
           "mapping malformed group rejected", code="malformed-projection-group")


def test_projection_scalar_item_is_fixed_error_not_invented_row():
    # a valid list group whose ENTRY is a scalar must not become a row built from an empty dict
    raises(ws.SearchError, lambda: ws.records_from_projection({"events": [7]}),
           "scalar event item rejected", code="malformed-projection-item")
    raises(ws.SearchError, lambda: ws.records_from_projection({"malformed": ["not-a-mapping"]}),
           "scalar malformed item rejected", code="malformed-projection-item")


def test_projection_over_bound_is_rejected_before_traversal():
    # the group is over-cap AND its entries are non-mappings: if the bound were checked AFTER traversal we
    # would see 'malformed-projection-item'; seeing 'too-many-records' proves the bound is enforced FIRST,
    # from the declared group length, before any entry is traversed or any row allocated.
    over = {"events": [0] * (ws.MAX_RECORDS + 1)}
    raises(ws.SearchError, lambda: ws.records_from_projection(over),
           "over-cap projection rejected before traversal", code="too-many-records")


def test_valid_projection_still_builds_and_searches_positive_control():
    # positive control preserved: a well-formed projection still builds correct rows and searches correctly
    projection = {
        "events": [{"line": 1, "category": "EVILTWIN", "ts": 10, "epoch": 20},
                   {"line": 4, "category": "PROBE_FLOOD", "ts": 40, "epoch": 50}],
        "unsupported": [{"line": 2, "reason": "unsupported-type"}],
        "malformed": [{"line": 3, "reason": "invalid-json"}],
    }
    rows = ws.records_from_projection(projection)
    eq(len(rows), 4, "one row per valid source entry, none invented or dropped")
    eq([r["status"] for r in rows], ["admitted", "admitted", "unsupported", "malformed"],
       "group order preserved: events first, then unsupported, then malformed")
    res = ws.search_records(rows, text="flood", status="admitted")
    eq(lines(res), [4], "valid projection still searches correctly end-to-end")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}", file=sys.stderr)
        except Exception as e:  # unexpected error is a failure too
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}", file=sys.stderr)
    print(f"ran {len(tests)} tests, {_ASSERTIONS} assertions, {failures} failure(s)")
    if failures:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
