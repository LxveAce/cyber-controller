"""Standalone tests for report_preview (no pytest, no package import).

Loads ``src/core/report_preview.py`` directly by FILE PATH via importlib, so it runs without any
``src.core`` package initializer, conftest, or app import. Real assertions; direct execution exits
nonzero on the first failure and zero when every check passes.

Import-safety: importing this file under a non-main name only DEFINES the ``test_*`` functions — it
runs no assertion, no print, and no ``sys.exit``, and it does not even load the module under test
(that happens lazily inside the first test that runs). The candidate module is loaded by an explicit
spec whose ``spec`` AND ``spec.loader`` are both checked before use.

Run:  python test_report_preview.py
"""

from __future__ import annotations

import importlib.util
import json
import math
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_MODULE_PATH = _HERE.parent / "src" / "core" / "report_preview.py"

# Generic, FICTIONAL identities used only to prove the leak-detection controls are meaningful. None of
# these is a real home path, username, or secret — they exist so the "withheld values never appear in
# the output" assertions have something concrete to look for.
FAKE_USERNAME = "example-user"
FAKE_HOME_PATH = "C:/Users/example-user"
FAKE_TOKEN = "fake-token-DO-NOT-USE-0000"

_rp_cache = None


def _load_module():
    """Load the candidate module by file path, checking BOTH the spec and its loader explicitly."""
    spec = importlib.util.spec_from_file_location("report_preview_under_test", _MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not build an import spec (or loader) for {_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rp():
    """Return the module under test, loading it lazily on first use (keeps import side-effect-free)."""
    global _rp_cache
    if _rp_cache is None:
        _rp_cache = _load_module()
    return _rp_cache


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# --------------------------------------------------------------------------------------------------
# Positive controls (behaviour that must keep working).
# --------------------------------------------------------------------------------------------------

def test_allowlisted_keys_all_included():
    rp = _rp()
    p = rp.build_preview({"app": "Cyber Controller", "version": "1.8.0", "status": "flash_failed"})
    _check(p["format"] == "cc-report-preview-v1", "format tag wrong")
    _check(
        p["included"] == {"app": "Cyber Controller", "status": "flash_failed", "version": "1.8.0"},
        f"included wrong: {p['included']}",
    )
    _check(p["omitted"] == [], f"omitted should be empty: {p['omitted']}")


def test_off_allowlist_values_never_leak():
    rp = _rp()
    p = rp.build_preview({
        "app": "CC",
        "username": FAKE_USERNAME,
        "home_path": FAKE_HOME_PATH,
        "auth_token": FAKE_TOKEN,
    })
    _check(p["included"] == {"app": "CC"}, f"only app should be included: {p['included']}")
    _check(
        p["omitted"] == ["auth_token", "home_path", "username"],
        f"omitted list wrong: {p['omitted']}",
    )
    _check(
        all(p["omitted_detail"][k] == "not_allowlisted" for k in ("auth_token", "home_path", "username")),
        "off-allowlist reason wrong",
    )
    # The withheld VALUES must appear nowhere in the serialized preview (leak-detection control).
    blob = json.dumps(p)
    for leaked in (FAKE_USERNAME, FAKE_HOME_PATH, FAKE_TOKEN):
        _check(leaked not in blob, f"withheld value leaked into preview: {leaked}")


def test_empty_facts_invents_nothing():
    rp = _rp()
    p = rp.build_preview({})
    _check(p["included"] == {}, "empty facts must give empty included")
    _check(p["omitted"] == [], "empty facts must give empty omitted")
    _check(
        "version" not in p["included"] and "app" not in p["included"],
        "module must not auto-fill any allowlisted field it was not given",
    )


def test_output_is_order_independent():
    rp = _rp()
    a = rp.build_preview({"version": "2", "app": "x", "status": "ok", "junk": 1})
    b = rp.build_preview({"junk": 1, "status": "ok", "app": "x", "version": "2"})
    _check(json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True), "output not order-independent")


def test_unsupported_structured_types_omitted():
    rp = _rp()
    p = rp.build_preview({"version": {"nested": 1}, "app": ["a", "b"]})
    _check(p["included"] == {}, f"structured values must not be included: {p['included']}")
    _check(p["omitted"] == ["app", "version"], f"omitted wrong: {p['omitted']}")
    _check(p["omitted_detail"]["version"] == "unsupported_type", "dict value reason wrong")
    _check(p["omitted_detail"]["app"] == "unsupported_type", "list value reason wrong")


def test_over_cap_string_omitted_not_truncated():
    rp = _rp()
    cap = rp.MAX_VALUE_BYTES
    at_cap = "v" * cap
    over_cap = "v" * (cap + 1)
    p = rp.build_preview({"version": at_cap, "build": over_cap})
    _check(p["included"] == {"version": at_cap}, "at-cap string should be included whole")
    _check(p["omitted"] == ["build"], f"over-cap should be omitted: {p['omitted']}")
    _check(p["omitted_detail"]["build"] == "too_long", "over-cap reason wrong")
    _check("build" not in json.dumps(p["included"]), "over-cap value must not appear included")


def test_scalar_none_bool_int_float_and_nan_inf():
    rp = _rp()
    p = rp.build_preview(
        {"status": None, "error_code": 42, "build": True, "version": 1.5},
        allow=("status", "error_code", "build", "version"),
    )
    _check(
        p["included"] == {"build": True, "error_code": 42, "status": None, "version": 1.5},
        f"scalar handling wrong: {p['included']}",
    )
    p = rp.build_preview({"version": math.nan, "error_code": math.inf},
                         allow=("version", "error_code"))
    _check(p["included"] == {}, "NaN/inf must not be included")
    _check(p["omitted_detail"]["version"] == "unsupported_type", "NaN reason wrong")
    _check(p["omitted_detail"]["error_code"] == "unsupported_type", "inf reason wrong")


def test_custom_and_empty_allowlist():
    rp = _rp()
    p = rp.build_preview({"summary": "user typed text", "app": "CC"}, allow=("summary",))
    _check(p["included"] == {"summary": "user typed text"}, "custom allow not honored")
    _check(p["omitted"] == ["app"], "app should be omitted under custom allow")
    p = rp.build_preview({"app": "CC", "version": "1"}, allow=())
    _check(p["included"] == {}, "empty allowlist must include nothing")
    _check(p["omitted"] == ["app", "version"], "empty allowlist must omit all supplied keys")


def test_allow_is_sorted_and_deduped():
    rp = _rp()
    p = rp.build_preview({}, allow=("b", "a", "a", "c"))
    _check(p["allow"] == ["a", "b", "c"], f"allow not sorted/deduped: {p['allow']}")


def test_input_mapping_not_mutated():
    rp = _rp()
    src = {"app": "CC", "secret": "x"}
    snapshot = dict(src)
    rp.build_preview(src)
    _check(src == snapshot, "build_preview mutated its input")


def test_defensive_type_guards():
    rp = _rp()
    # Non-mapping facts must raise TypeError.
    for bad in (None, "app,version", 5):
        try:
            rp.build_preview(bad)
        except TypeError:
            pass
        else:
            raise AssertionError(f"expected TypeError for facts={bad!r}")
    # A bare-string allowlist must raise (not be iterated per character).
    try:
        rp.build_preview({}, allow="app")
    except TypeError:
        pass
    else:
        raise AssertionError("bare-string allow should raise TypeError")
    # A non-str allow key must raise.
    try:
        rp.build_preview({}, allow=("app", 3))
    except TypeError:
        pass
    else:
        raise AssertionError("non-str allow key should raise TypeError")


def test_non_str_key_withheld_with_placeholder():
    rp = _rp()
    p = rp.build_preview({7: "x", "app": "CC"})
    _check(p["included"] == {"app": "CC"}, "non-str key must not be included")
    _check(p["omitted"] == [rp.PLACEHOLDER_LABEL], f"non-str key label not sanitized: {p['omitted']}")
    _check(
        p["omitted_detail"][rp.PLACEHOLDER_LABEL] == "invalid_key",
        "non-str key reason missing/wrong",
    )
    # Whatever the label form, the preview must round-trip through UTF-8 without raising.
    rp.render_text(p).encode("utf-8")


def test_render_text_deterministic_and_no_leak():
    rp = _rp()
    p = rp.build_preview({"app": "CC", "version": "1.8.0", "username": FAKE_USERNAME})
    t1 = rp.render_text(p)
    t2 = rp.render_text(rp.build_preview({"version": "1.8.0", "username": FAKE_USERNAME, "app": "CC"}))
    _check(t1 == t2, "render_text not deterministic across input order")
    _check("## Included fields" in t1 and "## Omitted fields" in t1, "render_text missing sections")
    _check("username  -- not_allowlisted" in t1, "render_text must name the withheld key + reason")
    _check("no claim to detect or scrub" in t1, "render_text must carry the PII honesty note")
    _check(FAKE_USERNAME not in t1, "render_text leaked a withheld value")
    _check("app: CC" in t1 and "version: 1.8.0" in t1, "render_text missing included fields")


def test_render_text_none_placeholders():
    rp = _rp()
    _check("(none)" in rp.render_text(rp.build_preview({})),
           "empty preview should render (none) placeholders")


# --------------------------------------------------------------------------------------------------
# Negative controls for the specific defects repaired (root findings #3a and #3b).
# --------------------------------------------------------------------------------------------------

def test_huge_int_omitted_and_output_renderable():
    """Root #3a: a huge int must NOT be included; the preview must build AND render without raising."""
    rp = _rp()
    huge = 10 ** 5000  # far past CPython's int->str digit ceiling; str(huge) would raise ValueError
    p = rp.build_preview({"build": huge, "app": "CC"})
    _check("build" not in p["included"], "huge int must not be included")
    _check(p["included"] == {"app": "CC"}, f"only app should survive: {p['included']}")
    _check(p["omitted"] == ["build"], f"huge int should be omitted: {p['omitted']}")
    _check(p["omitted_detail"]["build"] == "too_long", "huge int reason wrong")
    # The huge value must not be materialized anywhere; building and rendering must not raise.
    text = rp.render_text(p)
    text.encode("utf-8")
    _check("build  -- too_long" in text, "render_text should name the omitted huge int")


def test_int_digit_boundary():
    """The decimal-width bound is exact: MAX_INT_DIGITS digits pass, one more is omitted."""
    rp = _rp()
    max_digits = rp.MAX_INT_DIGITS
    at_limit = 10 ** max_digits - 1        # exactly MAX_INT_DIGITS digits (all nines)
    over_limit = 10 ** max_digits          # MAX_INT_DIGITS + 1 digits
    p = rp.build_preview({"error_code": at_limit, "build": over_limit},
                         allow=("error_code", "build"))
    _check(p["included"] == {"error_code": at_limit}, f"at-limit int should be included: {p['included']}")
    _check(p["omitted"] == ["build"], f"over-limit int should be omitted: {p['omitted']}")
    _check(p["omitted_detail"]["build"] == "too_long", "over-limit int reason wrong")
    # The included int must render (str of a bounded int never trips the ceiling).
    _check(f"error_code: {at_limit}" in rp.render_text(p), "at-limit int must render")


def test_surrogate_label_sanitized_and_output_renderable():
    """Root #3b: an off-allowlist key carrying a lone surrogate must be sanitized, not copied through."""
    rp = _rp()
    bad_key = "lat\ud800lon"  # a valid Python str, but NOT UTF-8 encodable (lone surrogate)
    p = rp.build_preview({bad_key: "value", "app": "CC"})
    _check(p["included"] == {"app": "CC"}, "surrogate-key entry must not be included")
    _check(p["omitted"] == [rp.PLACEHOLDER_LABEL], f"surrogate label not sanitized: {p['omitted']}")
    _check(p["omitted_detail"][rp.PLACEHOLDER_LABEL] == "invalid_key", "surrogate label reason wrong")
    # The raw surrogate must never appear in any emitted label.
    _check(bad_key not in p["omitted"], "raw surrogate label copied into omitted")
    # Both the text rendering and a non-ASCII JSON dump must encode to UTF-8 without raising.
    rp.render_text(p).encode("utf-8")
    json.dumps(p, ensure_ascii=False).encode("utf-8")


def test_surrogate_string_value_omitted():
    """A surrogate-bearing VALUE on an allowlisted key is omitted (invalid_string), never included."""
    rp = _rp()
    p = rp.build_preview({"version": "1.\ud800"})
    _check(p["included"] == {}, "surrogate value must not be included")
    _check(p["omitted_detail"]["version"] == "invalid_string", "surrogate value reason wrong")
    rp.render_text(p).encode("utf-8")


def test_over_long_label_sanitized():
    """A key longer than MAX_KEY_BYTES is unrenderable-by-policy and reported under the placeholder."""
    rp = _rp()
    long_key = "k" * (rp.MAX_KEY_BYTES + 1)
    p = rp.build_preview({long_key: "value"})
    _check(p["omitted"] == [rp.PLACEHOLDER_LABEL], f"over-long key not sanitized: {p['omitted']}")
    _check(p["omitted_detail"][rp.PLACEHOLDER_LABEL] == "invalid_key", "over-long key reason wrong")
    _check(long_key not in json.dumps(p), "over-long key copied into output")


def test_surrogate_allowlist_entry_sanitized_in_output():
    """An allowlist entry that is itself unrenderable must be sanitized in the emitted ``allow`` list."""
    rp = _rp()
    p = rp.build_preview({"app": "CC"}, allow=("app", "b\ud800d"))
    _check("app" in p["allow"], "valid allow entry dropped")
    _check(rp.PLACEHOLDER_LABEL in p["allow"], "unrenderable allow entry not sanitized")
    _check("b\ud800d" not in p["allow"], "raw surrogate allow entry copied into output")
    # The whole object must encode to UTF-8 without raising.
    json.dumps(p, ensure_ascii=False).encode("utf-8")
    rp.render_text(p).encode("utf-8")


def test_too_many_fields_raises():
    """Bounded total: more than MAX_FIELDS supplied keys is a structural misuse and raises ValueError."""
    rp = _rp()
    too_many = {f"k{i}": i for i in range(rp.MAX_FIELDS + 1)}
    try:
        rp.build_preview(too_many)
    except ValueError as exc:
        _check("too_many_fields" in str(exc), f"unexpected too-many-fields message: {exc}")
    else:
        raise AssertionError("facts over MAX_FIELDS should raise ValueError")
    # Exactly MAX_FIELDS must still be accepted (boundary is inclusive of the limit).
    ok = {f"k{i}": i for i in range(rp.MAX_FIELDS)}
    rp.build_preview(ok)


def test_too_large_allowlist_raises():
    """Bounded total: an allowlist larger than MAX_ALLOW raises ValueError."""
    rp = _rp()
    big_allow = tuple(f"a{i}" for i in range(rp.MAX_ALLOW + 1))
    try:
        rp.build_preview({}, allow=big_allow)
    except ValueError as exc:
        _check("too_many_allow" in str(exc), f"unexpected too-many-allow message: {exc}")
    else:
        raise AssertionError("allow over MAX_ALLOW should raise ValueError")


# --------------------------------------------------------------------------------------------------
# Negative controls for root's TWO new 0038 edges.
# --------------------------------------------------------------------------------------------------

def test_placeholder_collision_order_independent():
    """New edge 1: a valid off-allowlist key literally equal to PLACEHOLDER_LABEL and an unrenderable
    (invalid) key both collapse to the same display label. The omitted/omitted_detail result must be
    IDENTICAL across every insertion order (resolved by fixed precedence, not by insertion order)."""
    rp = _rp()
    placeholder = rp.PLACEHOLDER_LABEL
    surrogate_key = "bad\ud800key"  # a valid Python str, NOT UTF-8 encodable -> sanitized to placeholder
    # Same contents, opposite insertion orders. One key is a real off-allowlist string spelled exactly
    # like the placeholder; the other is a genuinely unrenderable key that sanitizes to the placeholder.
    facts_a = {placeholder: "x", surrogate_key: "y", "app": "CC"}
    facts_b = {"app": "CC", surrogate_key: "y", placeholder: "x"}
    pa = rp.build_preview(facts_a)
    pb = rp.build_preview(facts_b)
    _check(pa["omitted"] == pb["omitted"],
           f"omitted not order-independent: {pa['omitted']} vs {pb['omitted']}")
    _check(pa["omitted_detail"] == pb["omitted_detail"],
           f"omitted_detail not order-independent: {pa['omitted_detail']} vs {pb['omitted_detail']}")
    _check(pa["included"] == {"app": "CC"}, f"only app should be included: {pa['included']}")
    _check(pa["omitted"] == [placeholder], f"omitted should be just the placeholder: {pa['omitted']}")
    _check(pa["omitted_detail"][placeholder] == "invalid_key",
           f"placeholder reason must be fixed (invalid_key wins): {pa['omitted_detail']}")
    # Full object determinism, and no raw surrogate bytes copied through into any label.
    _check(json.dumps(pa, sort_keys=True) == json.dumps(pb, sort_keys=True),
           "full preview object not order-independent")
    _check(surrogate_key not in pa["omitted"], "raw surrogate label copied into omitted")
    rp.render_text(pa).encode("utf-8")
    json.dumps(pa, ensure_ascii=False).encode("utf-8")
    # Precedence is genuine, not a hard-wire: with NO invalid key present, a real off-allowlist key
    # spelled like the placeholder still reports not_allowlisted.
    solo = rp.build_preview({placeholder: "x", "app": "CC"})
    _check(solo["omitted_detail"][placeholder] == "not_allowlisted",
           f"real placeholder-named key should read not_allowlisted when no invalid key present: "
           f"{solo['omitted_detail']}")


def test_over_cap_allow_bounded_rejection_no_hang():
    """New edge 2: an over-cap allowlist is a BOUNDED rejection, never a full materialization. Proven
    with an over-cap list AND an unbounded generator that would hang if fully consumed; a within-cap
    (and exactly-at-cap) allow still works exactly as before."""
    rp = _rp()
    cap = rp.MAX_ALLOW
    # (1) Over-cap concrete list -> ValueError (too_many_allow).
    over_list = [f"a{i}" for i in range(cap + 1)]
    try:
        rp.build_preview({}, allow=over_list)
    except ValueError as exc:
        _check("too_many_allow" in str(exc), f"unexpected over-cap list message: {exc}")
    else:
        raise AssertionError("over-cap list allow should raise ValueError")

    # (2) Unbounded generator: if _normalize_allow materialized it (list(allow)) this would never
    # return. A bounded consumer pulls at most cap + 1 entries, then rejects. We count how many entries
    # were actually pulled from the generator to prove consumption is bounded (and that nothing hung).
    pulled = 0

    def infinite_allow():
        nonlocal pulled
        i = 0
        while True:
            pulled += 1
            yield f"g{i}"
            i += 1

    try:
        rp.build_preview({}, allow=infinite_allow())
    except ValueError as exc:
        _check("too_many_allow" in str(exc), f"unexpected generator message: {exc}")
    else:
        raise AssertionError("unbounded generator allow should raise ValueError")
    _check(pulled <= cap + 1,
           f"generator over-consumed ({pulled} > {cap + 1}); consumption is not bounded")

    # (3) A within-cap allow behaves exactly as before: honored, sorted, de-duplicated.
    p = rp.build_preview({"summary": "text", "app": "CC"}, allow=("summary", "summary", "app"))
    _check(p["allow"] == ["app", "summary"], f"within-cap allow not sorted/deduped: {p['allow']}")
    _check(p["included"] == {"app": "CC", "summary": "text"},
           f"within-cap allow not honored: {p['included']}")
    # Exactly MAX_ALLOW entries is still accepted whole (inclusive boundary), via a generator too.
    at_cap = rp.build_preview({}, allow=(f"c{i}" for i in range(cap)))
    _check(len(at_cap["allow"]) == cap, f"at-cap allow should be accepted whole: {len(at_cap['allow'])}")


def _all_tests():
    return [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def _run_all():
    tests = _all_tests()
    for t in tests:
        t()
    return len(tests)


if __name__ == "__main__":
    count = _run_all()
    print(f"OK: {count} test functions passed")
    sys.exit(0)
