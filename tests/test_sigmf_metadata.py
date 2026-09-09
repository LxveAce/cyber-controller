"""Assertions for the sigmf_metadata module.

Import-safe: importing this file only loads the module under test (by explicit file path via importlib)
and defines functions -- it runs no assertions and never exits, so a test collector can import it. The
assertions live in the discoverable ``test_sigmf_metadata()`` function, which raises AssertionError on the
first failed check (a real failure a collector reports). Running the file directly executes the same
assertions and exits nonzero on failure. No pytest/conftest/app import, no ``src``/``core`` package
initializer, no network/filesystem-data/device/real-recording access.
"""

import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.join(_HERE, "..", "src", "core", "sigmf_metadata.py")
_spec = importlib.util.spec_from_file_location("sigmf_metadata_candidate", _MOD_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("cannot locate module under test at %s" % _MOD_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
summarize = _mod.summarize_sigmf_metadata

_passed = [0]


def check(name, cond, detail: object = ""):
    """A real assertion: raise AssertionError on failure (so a collector reports it); count on pass."""
    if not cond:
        raise AssertionError("%s -- %s" % (name, detail))
    _passed[0] += 1
    print("PASS  " + name)


def base():
    return {
        "global": {"core:datatype": "cf32_le", "core:version": "1.2.6"},
        "captures": [{"core:sample_start": 0}],
        "annotations": [],
    }


def J(d):
    return json.dumps(d)


def codes(r):
    return {d["code"] for d in r["diagnostics"]}


def test_sigmf_metadata():
    """Discoverable test entrypoint. Runs every assertion; raises AssertionError on any failure."""
    _passed[0] = 0

    # --- minimal valid summary + expected output ---
    r = summarize(J(base()))
    check("minimal valid -> summarized", r["status"] == "summarized", r)
    check("summary format/version", r["summary"]["format"] == "SigMF" and r["summary"]["version"] == "1.2.6", r["summary"])
    dt = r["summary"]["datatype"]
    check("datatype cf32_le derivations", dt["complex"] is True and dt["element_format"] == "f32"
          and dt["bytes_per_channel_sample"] == 8 and dt["byte_order"] == "little", dt)
    check("summarized has empty diagnostics", r["diagnostics"] == [] and r["diagnostics_truncated"] is False, r)

    # --- bytes as input ---
    check("bytes input summarized", summarize(J(base()).encode("utf-8"))["status"] == "summarized")

    # --- supported / unsupported / malformed version ---
    b = base(); b["global"]["core:version"] = "1.0.0"
    r = summarize(J(b)); check("well-formed other version -> unsupported", r["status"] == "unsupported" and r["summary"] is None, r)
    b = base(); b["global"]["core:version"] = "1.2"
    check("malformed version -> invalid", summarize(J(b))["status"] == "invalid")
    b = base(); b["global"]["core:version"] = "2.0.0"
    check("future version -> unsupported", summarize(J(b))["status"] == "unsupported")

    # --- missing required / bad top-level ---
    b = base(); del b["global"]["core:datatype"]
    check("missing datatype -> invalid", summarize(J(b))["status"] == "invalid")
    b = base(); del b["captures"]
    check("missing captures -> invalid", summarize(J(b))["status"] == "invalid")
    check("top-level array -> invalid", summarize("[]")["status"] == "invalid")
    check("top-level non-json -> invalid", summarize("not json")["status"] == "invalid")

    # --- datatype byte size / endianness matrix ---
    def dt_of(token):
        b = base(); b["global"]["core:datatype"] = token
        return summarize(J(b))

    r = dt_of("ru16_be"); check("ru16_be", r["status"] == "summarized" and r["summary"]["datatype"]["complex"] is False
          and r["summary"]["datatype"]["bytes_per_channel_sample"] == 2 and r["summary"]["datatype"]["byte_order"] == "big", r)
    r = dt_of("cu8"); check("cu8 complex byte inapplicable order", r["status"] == "summarized"
          and r["summary"]["datatype"]["bytes_per_channel_sample"] == 2 and r["summary"]["datatype"]["byte_order"] == "inapplicable", r)
    r = dt_of("ci8"); check("ci8 complex byte", r["status"] == "summarized" and r["summary"]["datatype"]["bytes_per_channel_sample"] == 2, r)
    r = dt_of("rf64_le"); check("rf64_le real 8-byte", r["status"] == "summarized" and r["summary"]["datatype"]["bytes_per_channel_sample"] == 8, r)
    check("cf32 (multibyte no endianness) -> invalid", dt_of("cf32")["status"] == "invalid")
    check("cu8_le (byte with endianness) -> invalid", dt_of("cu8_le")["status"] == "invalid")
    check("zf32_le (bad prefix) -> invalid", dt_of("zf32_le")["status"] == "invalid")
    check("empty datatype -> invalid", dt_of("")["status"] == "invalid")

    # --- numeric type / ranges (bool excluded, exclusive-min, max) ---
    def with_global(**kv):
        b = base(); b["global"].update(kv); return summarize(J(b))

    check("sample_rate 1e6 summarized", with_global(**{"core:sample_rate": 1000000})["status"] == "summarized")
    check("sample_rate 0 -> invalid (exclusive min)", with_global(**{"core:sample_rate": 0})["status"] == "invalid")
    check("sample_rate -1 -> invalid", with_global(**{"core:sample_rate": -1})["status"] == "invalid")
    check("sample_rate 2e12 -> invalid (over max)", with_global(**{"core:sample_rate": 2000000000000})["status"] == "invalid")
    check("num_channels 0 -> invalid", with_global(**{"core:num_channels": 0})["status"] == "invalid")
    check("num_channels 1.5 -> invalid (not int)", with_global(**{"core:num_channels": 1.5})["status"] == "invalid")
    check("num_channels bool -> invalid", with_global(**{"core:num_channels": True})["status"] == "invalid")
    r = with_global(**{"core:num_channels": 2})
    check("num_channels 2 summarized value", r["status"] == "summarized" and r["summary"]["num_channels"] == 2, r)
    check("sha512 wrong shape -> invalid", with_global(**{"core:sha512": "abc"})["status"] == "invalid")

    # --- malformed containers / duplicate keys / non-finite ---
    b = base(); b["global"] = []
    check("global not object -> invalid", summarize(J(b))["status"] == "invalid")
    b = base(); b["captures"] = {}
    check("captures not array -> invalid", summarize(J(b))["status"] == "invalid")
    dup = '{"global":{"core:datatype":"cf32_le","core:version":"1.2.6","core:hw":"a","core:hw":"b"},"captures":[{"core:sample_start":0}],"annotations":[]}'
    r = summarize(dup); check("duplicate key -> invalid", r["status"] == "invalid" and "duplicate_key" in codes(r), r)
    nan = '{"global":{"core:datatype":"cf32_le","core:version":"1.2.6","core:sample_rate":NaN},"captures":[{"core:sample_start":0}],"annotations":[]}'
    r = summarize(nan); check("non-finite number -> invalid", r["status"] == "invalid" and "non_finite_number" in codes(r), r)

    # --- ordering: captures AND annotations MUST ascend by core:sample_start ---
    b = base(); b["captures"] = [{"core:sample_start": 5}, {"core:sample_start": 1}]
    r = summarize(J(b)); check("captures out of order -> invalid", r["status"] == "invalid" and "order" in codes(r), r)
    b = base(); b["annotations"] = [{"core:sample_start": 5}, {"core:sample_start": 1}]
    r = summarize(J(b))
    check("annotations out of order -> invalid (MUST ascend)", r["status"] == "invalid" and "order" in codes(r), r)

    # --- extensions: required unsupported / optional opaque / malformed invalid ---
    b = base(); b["global"]["core:extensions"] = [{"name": "x", "version": "1.0.0", "optional": False}]
    check("required extension -> unsupported", summarize(J(b))["status"] == "unsupported")
    b = base(); b["global"]["core:extensions"] = [{"name": "x", "version": "1.0.0", "optional": True}]
    r = summarize(J(b))
    check("optional extension -> summarized opaque", r["status"] == "summarized"
          and len(r["summary"]["extensions_opaque"]) == 1 and r["summary"]["extensions_opaque"][0]["interpreted"] is False, r)
    b = base(); b["global"]["core:extensions"] = [{"name": "x", "version": "1.0.0"}]
    check("extension missing field -> invalid", summarize(J(b))["status"] == "invalid")
    b = base(); b["global"]["core:extensions"] = [{"name": "x", "version": "1.0.0", "optional": True, "extra": 1}]
    check("extension extra field -> invalid", summarize(J(b))["status"] == "invalid")

    # --- dataset + metadata_only combination -> unsupported; dataset alone inert ---
    b = base(); b["global"]["core:dataset"] = "rec.sigmf-data"; b["global"]["core:metadata_only"] = True
    check("dataset + metadata_only:true -> unsupported", summarize(J(b))["status"] == "unsupported")
    b = base(); b["global"]["core:dataset"] = "rec.sigmf-data"; b["global"]["core:license"] = "https://example.com/x"
    r = summarize(J(b))
    check("inert filename + URL preserved, never followed", r["status"] == "summarized"
          and r["summary"]["inert_references"]["core:dataset"] == "rec.sigmf-data"
          and r["summary"]["inert_references"]["core:license"] == "https://example.com/x", r)

    # --- unknown core field + non-core namespace preserved uninterpreted WITH value ---
    b = base(); b["global"]["core:brand_new_field"] = "z"; b["global"]["vendor:foo"] = 1
    r = summarize(J(b))
    ug = r["summary"]["uninterpreted_global"]
    check("unknown/non-core preserved with value", r["status"] == "summarized"
          and ug.get("core:brand_new_field", {}).get("value") == "z"
          and ug.get("core:brand_new_field", {}).get("semantics") == "deferred"
          and ug.get("vendor:foo", {}).get("value") == 1
          and ug.get("vendor:foo", {}).get("semantics") == "unknown", ug)

    # --- resource / bound limits ---
    check("oversized input -> invalid", summarize("x" * 262145)["status"] == "invalid")
    check("deep nesting -> invalid", summarize("[" * 40 + "]" * 40)["status"] == "invalid")
    check("too many total values -> invalid", summarize("[" + ",".join("0" for _ in range(40000)) + "]")["status"] == "invalid")
    b = base(); b["global"]["core:author"] = "a" * 9000
    check("over-long string -> invalid", summarize(J(b))["status"] == "invalid")
    b = base()
    for i in range(130):
        b["global"]["k%d" % i] = 1
    check("too many keys in an object -> invalid", summarize(J(b))["status"] == "invalid")

    # --- diagnostics are bounded (<=16) with a truncation flag ---
    b = base(); b["annotations"] = [{"core:sample_start": 0, "core:sample_count": -1} for _ in range(20)]
    r = summarize(J(b))
    check("diagnostics bounded to 16 + truncated flag", r["status"] == "invalid"
          and len(r["diagnostics"]) == 16 and r["diagnostics_truncated"] is True, (len(r["diagnostics"]), r["diagnostics_truncated"]))
    check("diagnostics never dump raw input", all(len(d["detail"]) <= 160 for d in r["diagnostics"]), r["diagnostics"])

    # --- empty captures documented default, no invented values ---
    b = base(); b["captures"] = []
    r = summarize(J(b))
    check("empty captures handled, no invented frequency", r["status"] == "summarized"
          and r["summary"]["captures"]["count"] == 0 and r["summary"]["captures"]["empty"] is True
          and "core:frequency" not in json.dumps(r["summary"]), r["summary"]["captures"])

    # --- boundary + preservation regressions ---
    # escaped/unpaired surrogate in a value or key -> compact invalid, no crash
    check("escaped surrogate value -> invalid",
          summarize('{"global":{"core:datatype":"cf32_le","core:version":"1.2.6","core:author":"\\ud800"},'
                    '"captures":[{"core:sample_start":0}],"annotations":[]}')["status"] == "invalid")
    check("escaped surrogate key -> invalid",
          summarize('{"global":{"core:datatype":"cf32_le","core:version":"1.2.6","\\ud800":"x"},'
                    '"captures":[{"core:sample_start":0}],"annotations":[]}')["status"] == "invalid")
    # parsed-float exponent overflow anywhere (incl uninterpreted) -> invalid
    check("1e999 overflow in uninterpreted -> invalid",
          summarize('{"global":{"core:datatype":"cf32_le","core:version":"1.2.6","vendor:v":1e999},'
                    '"captures":[{"core:sample_start":0}],"annotations":[]}')["status"] == "invalid")
    # trailing newline must NOT complete a token
    check("datatype trailing newline -> invalid",
          summarize('{"global":{"core:datatype":"cf32_le\\n","core:version":"1.2.6"},'
                    '"captures":[{"core:sample_start":0}],"annotations":[]}')["status"] == "invalid")
    check("version trailing newline -> invalid",
          summarize('{"global":{"core:datatype":"cf32_le","core:version":"1.2.6\\n"},'
                    '"captures":[{"core:sample_start":0}],"annotations":[]}')["status"] == "invalid")
    b = base(); b["global"]["core:sha512"] = "a" * 128 + "\n"
    check("sha512 trailing newline -> invalid", summarize(J(b))["status"] == "invalid")
    b = base(); b["global"]["core:sha512"] = "a" * 128
    check("sha512 exact 128 hex -> summarized", summarize(J(b))["status"] == "summarized")
    # dataset + metadata_only present together -> unsupported regardless of the boolean value
    b = base(); b["global"]["core:dataset"] = "a.sigmf-data"; b["global"]["core:metadata_only"] = False
    check("dataset + metadata_only:false -> unsupported", summarize(J(b))["status"] == "unsupported")
    # values are PRESERVED (global vendor note, capture frequency/datetime, annotation descriptors)
    b = base(); b["global"]["vendor:note"] = "hello"
    b["captures"] = [{"core:sample_start": 0, "core:frequency": 915000000, "core:datetime": "2020-01-01T00:00:00Z"}]
    r = summarize(J(b))
    check("global vendor note value survives",
          r["status"] == "summarized" and r["summary"]["uninterpreted_global"].get("vendor:note", {}).get("value") == "hello",
          r["summary"].get("uninterpreted_global"))
    ent = r["summary"]["captures"]["entries"][0]
    check("capture frequency survives (recognized)", ent["recognized"].get("core:frequency") == 915000000, ent)
    check("capture datetime preserved uninterpreted+deferred",
          ent["uninterpreted"].get("core:datetime", {}).get("value") == "2020-01-01T00:00:00Z"
          and ent["uninterpreted"]["core:datetime"]["semantics"] == "deferred", ent)
    b = base(); b["annotations"] = [{"core:sample_start": 0, "core:sample_count": 10, "core:label": "sig", "vendor:x": 7}]
    r = summarize(J(b)); ae = r["summary"]["annotations"]["entries"][0]
    check("annotation sample_count survives (recognized)", r["status"] == "summarized" and ae["recognized"].get("core:sample_count") == 10, ae)
    check("annotation label preserved uninterpreted", ae["uninterpreted"].get("core:label", {}).get("value") == "sig", ae)

    def _nested(levels):
        obj = 0
        for _ in range(levels):
            obj = {"n": obj}
        return obj

    # depth counts CONTAINERS (root=1): root(1)+global(2)+N nested containers -> deepest container = 2+N.
    b = base(); b["global"]["vendor:deep"] = _nested(30)   # deepest container level == 32 (exact boundary)
    check("exactly 32 container levels -> summarized", summarize(J(b))["status"] == "summarized")
    b = base(); b["global"]["vendor:deep"] = _nested(31)   # deepest container level == 33 (one beyond)
    r = summarize(J(b))
    check("33 container levels -> invalid", r["status"] == "invalid" and "max_depth" in codes(r), r)


if __name__ == "__main__":
    try:
        test_sigmf_metadata()
    except AssertionError as exc:
        print("\nFAIL  " + str(exc))
        sys.exit(1)
    print("\nALL PASS (%d assertions)" % _passed[0])
    sys.exit(0)
