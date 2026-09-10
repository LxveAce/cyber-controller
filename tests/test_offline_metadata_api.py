"""Isolated tests for offline_metadata_api (the Offline Data summarize route's request-shape handler).

Loads the handler AND the accepted SigMF parser BY FILE PATH via importlib (relative to this file, matching
the repo layout). It does NOT import the Flask app/router/package initializer, and does not run pytest/conftest
or any network/device path. Import-safe: importing this file runs no assertions and never exits; the
discoverable ``test_offline_metadata_api()`` function raises AssertionError on the first failed check; running
the file directly executes the same assertions and exits nonzero on failure.
"""

import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(mod_name, rel_path):
    path = os.path.join(_HERE, *rel_path)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot locate %s at %s" % (mod_name, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


api = _load("offline_metadata_api_candidate", ("..", "src", "ui", "web", "offline_metadata_api.py"))
parser = _load("sigmf_metadata_for_api_test", ("..", "src", "core", "sigmf_metadata.py"))
summarize = parser.summarize_sigmf_metadata

_passed = [0]


def check(name, cond, detail: object = ""):
    if not cond:
        raise AssertionError("%s -- %s" % (name, detail))
    _passed[0] += 1
    print("PASS  " + name)


def reader_for(data: bytes):
    """A conformant bounded reader over `data` that never returns more than requested."""
    box = {"pos": 0}

    def read_body(limit):
        start = box["pos"]
        chunk = data[start:start + limit]
        box["pos"] = start + len(chunk)
        return chunk

    return read_body


def call(data=b"", *, content_type="application/octet-stream", content_length=None, read_body=None):
    if content_length is None:
        content_length = len(data)
    if read_body is None:
        read_body = reader_for(data)
    return api.summarize_response(content_type=content_type, content_length=content_length,
                                  read_body=read_body, summarize=summarize)


def minimal_meta():
    return json.dumps({
        "global": {"core:datatype": "cf32_le", "core:version": "1.2.6"},
        "captures": [{"core:sample_start": 0}],
        "annotations": [],
    }).encode("utf-8")


def body_json(result):
    status, body, headers = result
    return status, json.loads(body.decode("utf-8")), headers


def test_offline_metadata_api():
    _passed[0] = 0

    # --- validated_content_length (raw CONTENT_LENGTH strict parse) ---
    check("valid length parses", api.validated_content_length("100") == 100)
    check("leading zeros normalized", api.validated_content_length("00042") == 42)
    check("zero stays a valid empty body", api.validated_content_length("0") == 0)
    check("non-digit -> None", api.validated_content_length("abc") is None)
    check("empty -> None", api.validated_content_length("") is None)
    check("signed -> None", api.validated_content_length("-1") is None)
    check("None input -> None", api.validated_content_length(None) is None)
    check("at cap parses exactly", api.validated_content_length(str(api.MAX_DESCRIPTOR_BYTES)) == api.MAX_DESCRIPTOR_BYTES)
    check("over cap saturates to cap+1", api.validated_content_length(str(api.MAX_DESCRIPTOR_BYTES + 5)) == api.MAX_DESCRIPTOR_BYTES + 1)

    # --- transport / framing errors (fixed codes, no-store, no input echo) ---
    st, b, h = body_json(call(minimal_meta(), content_type="text/plain"))
    check("wrong content-type -> 415", st == 415 and b["error"] == "unsupported-content-type")
    check("415 no-store header", h.get("Cache-Control") == "no-store")
    st, b, _ = body_json(call(minimal_meta(), content_length=None if False else -1))
    check("negative length -> 411", st == 411 and b["error"] == "length-required")
    st, b, _ = body_json(api.summarize_response(content_type="application/octet-stream", content_length=None,
                                                read_body=reader_for(b"x"), summarize=summarize))
    check("None length -> 411", st == 411 and b["error"] == "length-required")
    st, b, _ = body_json(call(b"x" * 10, content_length=api.MAX_DESCRIPTOR_BYTES + 1))
    check("over-cap length -> 413 before read", st == 413 and b["error"] == "payload-too-large")
    # premature EOF: declared 20 but reader yields only 5 then EOF
    st, b, _ = body_json(call(content_length=20, read_body=reader_for(b"12345")))
    check("short body -> 400 incomplete-body", st == 400 and b["error"] == "incomplete-body")
    # non-conformant reader returns MORE than requested
    st, b, _ = body_json(api.summarize_response(content_type="application/octet-stream", content_length=4,
                                                read_body=lambda limit: b"x" * (limit + 3), summarize=summarize))
    check("over-returning reader -> 400 invalid-body", st == 400 and b["error"] == "invalid-body")

    # --- processed results are HTTP 200 with the parser status in the body ---
    st, b, h = body_json(call(minimal_meta()))
    check("valid metadata -> 200 summarized", st == 200 and b["status"] == "summarized", b)
    check("200 no-store header", h.get("Cache-Control") == "no-store")
    check("200 json content-type", h.get("Content-Type") == "application/json")
    check("summarized body carries datatype derivation", b["summary"]["datatype"]["bytes_per_channel_sample"] == 8, b["summary"]["datatype"])

    unsup = json.dumps({"global": {"core:datatype": "cf32_le", "core:version": "1.0.0"},
                        "captures": [{"core:sample_start": 0}], "annotations": []}).encode("utf-8")
    st, b, _ = body_json(call(unsup))
    check("unsupported version -> 200 unsupported", st == 200 and b["status"] == "unsupported", b)

    st, b, _ = body_json(call(b"not json"))
    check("malformed -> 200 invalid", st == 200 and b["status"] == "invalid", b)

    # N == 0: empty body summarized normally (empty is not valid SigMF JSON -> invalid), still HTTP 200
    st, b, _ = body_json(call(b"", content_length=0))
    check("empty body -> 200 invalid (no read, summarized)", st == 200 and b["status"] == "invalid", b)

    # exact-cap boundary: a body exactly at the cap is read (here a small valid body declared at len)
    st, b, _ = body_json(call(minimal_meta(), content_length=len(minimal_meta())))
    check("exact declared length reads fully -> summarized", st == 200 and b["status"] == "summarized", b)

    # false-positive control: a wrong-fork-ish datatype token is rejected by the parser (invalid), not summarized
    baddt = json.dumps({"global": {"core:datatype": "cf32", "core:version": "1.2.6"},
                        "captures": [{"core:sample_start": 0}], "annotations": []}).encode("utf-8")
    st, b, _ = body_json(call(baddt))
    check("incomplete datatype token -> 200 invalid (false-positive control)", st == 200 and b["status"] == "invalid", b)

    # large integer beyond JS safe range -> emitted as an EXACT decimal STRING (lossless display contract),
    # so the browser's JSON.parse cannot round it. A small int stays a JSON number.
    big = json.dumps({"global": {"core:datatype": "cf32_le", "core:version": "1.2.6"},
                      "captures": [{"core:sample_start": 9007199254740993}], "annotations": []}).encode("utf-8")
    raw = api.summarize_response(content_type="application/octet-stream", content_length=len(big),
                                 read_body=reader_for(big), summarize=summarize)[1].decode("utf-8")
    check("large int emitted as exact decimal string", '"9007199254740993"' in raw, raw[:220])
    parsed = json.loads(raw)
    check("large int lossless as string in body", parsed["summary"]["captures"]["entries"][0]["recognized"]["core:sample_start"] == "9007199254740993", parsed["summary"]["captures"]["entries"][0])
    # a small int stays a number (not stringified)
    small_raw = body_json(call(minimal_meta()))
    small_body = small_raw[1]
    check("small int stays a JSON number", small_body["summary"]["captures"]["entries"][0]["recognized"]["core:sample_start"] == 0, small_body["summary"]["captures"]["entries"][0])

    # the handler sends nothing outward: its source imports no network client
    src = open(os.path.join(_HERE, "..", "src", "ui", "web", "offline_metadata_api.py"), encoding="utf-8").read()
    check("handler imports no network client", not any(x in src for x in ("import requests", "urllib", "http.client", "socket")), "network import present")


if __name__ == "__main__":
    try:
        test_offline_metadata_api()
    except AssertionError as exc:
        print("\nFAIL  " + str(exc))
        sys.exit(1)
    print("\nALL PASS (%d assertions)" % _passed[0])
    sys.exit(0)
