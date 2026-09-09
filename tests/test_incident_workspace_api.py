"""Tests for the offline incident-import route handler. Synthetic bytes only; no real app/socket/device and no
real logs. Two layers:

* the pure ``import_response()`` driven directly with a STRICT in-memory stream that models a PEP-3333 WSGI
  input: it serves at most the declared framing and REJECTS any read request larger than the bytes still
  declared, so a handler that probed past ``Content-Length`` (the old cap+1 mistake) would fail the test. It
  proves declared-length-only reads, partial-read accumulation, premature EOF, and the zero-length no-read case.
* an isolated Flask test client that registers ONLY this route under a STAND-IN auth/CSRF wrapper. That is
  STATIC wiring — a stand-in, NOT the real ``requires_auth``/``requires_csrf`` decorators exercised — and is
  labelled so. The isolated app SETS ``MAX_CONTENT_LENGTH = 256 KiB`` (as the real app does) so the tests PROVE
  a valid >256 KiB and <=2 MiB upload reaches the handler via the raw ``wsgi.input`` read (not blocked by the
  global cap), and that an over-2 MiB declaration is rejected by the route's own fixed 413. The permissive
  test-client stream cannot establish PEP-3333 conformance — that is the strict-stream layer's job.
"""
from __future__ import annotations

import json
from typing import Optional

import pytest

from src.core.incident_export import export_bytes
from src.core.incident_import import import_incidents
from src.ui.web.incident_workspace_api import ROUTE_MAX_BYTES, import_response, validated_content_length

OCTET = "application/octet-stream"
GLOBAL_CAP = 256 * 1024


class _DeclaredStream:
    """A PEP-3333-conformant WSGI input model. Serves at most the framed body in chunks and REJECTS any read
    request larger than the bytes remaining in the declared framing — the handler must never over-read past
    Content-Length. ``available`` bytes may be fewer than ``declared`` to simulate a premature EOF (a client
    that disconnected mid-upload)."""

    def __init__(self, available: bytes, declared: int, chunk: int = 1 << 30):
        self._available = available
        self._declared = declared
        self._served = 0
        self._chunk = chunk
        self.reads = 0

    def read(self, k: int) -> bytes:
        self.reads += 1
        remaining = self._declared - self._served
        assert 0 <= k <= remaining, f"over-read: asked {k} with {remaining} declared bytes remaining"
        take = min(k, self._chunk, len(self._available) - self._served)
        if take <= 0:
            return b""                       # EOF: declared end, or premature if available < declared
        out = self._available[self._served:self._served + take]
        self._served += take
        return out


def _stream(data: bytes, *, declared: Optional[int] = None, chunk: int = 1 << 30) -> _DeclaredStream:
    return _DeclaredStream(data, len(data) if declared is None else declared, chunk)


def _supported_line() -> bytes:
    return (json.dumps({"ts": 1, "epoch": 1_700_000_000, "node": "!a", "src": "s", "type": "EVILTWIN",
                        "raw": "ssid=x"}) + "\n").encode("utf-8")


def _no_read(_limit):
    raise AssertionError("read_body must not be called")


# ── the strict stream is a real oracle (proves the over-read guard is not vacuous) ────────────────────

def test_declared_stream_rejects_a_read_past_the_declared_framing():
    s = _stream(b"abc")
    with pytest.raises(AssertionError):
        s.read(4)                            # asking for more than the 3 declared bytes is rejected


# ── content type ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ct", [None, "", "text/plain", "application/json", "multipart/form-data"])
def test_unsupported_content_type_is_415(ct):
    status, body, headers = import_response(content_type=ct, content_length=10, read_body=_no_read)
    assert status == 415 and json.loads(body) == {"error": "unsupported-content-type"}
    assert headers["Cache-Control"] == "no-store"


def test_octet_stream_with_charset_param_is_accepted():
    data = _supported_line()
    status, _, _ = import_response(content_type="application/octet-stream; charset=binary",
                                   content_length=len(data), read_body=_stream(data).read)
    assert status == 200


# ── content length (validated before reading) ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("cl", [None, -1, "10", 1.5, True])
def test_missing_or_invalid_content_length_is_411_and_body_never_read(cl):
    status, body, _ = import_response(content_type=OCTET, content_length=cl, read_body=_no_read)
    assert status == 411 and json.loads(body) == {"error": "length-required"}


def test_declared_length_over_cap_is_413_and_body_never_read():
    status, body, _ = import_response(content_type=OCTET, content_length=ROUTE_MAX_BYTES + 1, read_body=_no_read)
    assert status == 413 and json.loads(body) == {"error": "payload-too-large"}


# ── declared-length-only reads: accepted, partial, premature EOF, zero-length ─────────────────────────

def test_accepted_body_returns_exact_export_bytes_and_no_store():
    data = _supported_line() + _supported_line()
    s = _stream(data)
    status, body, headers = import_response(content_type=OCTET, content_length=len(data), read_body=s.read)
    assert status == 200
    assert body == export_bytes(import_incidents(data))     # exact accepted redacted projection
    assert headers == {"Content-Type": "application/json", "Cache-Control": "no-store"}


def test_partial_reads_are_accumulated_without_over_reading():
    data = _supported_line() * 5
    s = _stream(data, chunk=7)                              # tiny chunks force many bounded reads
    status, body, _ = import_response(content_type=OCTET, content_length=len(data), read_body=s.read)
    assert status == 200 and body == export_bytes(import_incidents(data))
    assert s.reads > 1                                       # actually exercised the accumulation loop


def test_premature_eof_before_declared_length_is_incomplete_body():
    data = _supported_line()
    # server framed len(data)+50 but only len(data) bytes actually arrive -> EOF before N -> incomplete
    s = _stream(data, declared=len(data) + 50)
    status, body, _ = import_response(content_type=OCTET, content_length=len(data) + 50, read_body=s.read)
    assert status == 400 and json.loads(body) == {"error": "incomplete-body"}


def test_zero_length_reads_nothing_and_is_a_real_empty_report():
    status, body, _ = import_response(content_type=OCTET, content_length=0, read_body=_no_read)
    assert status == 200
    proj = json.loads(body)
    assert proj["coverage"]["admitted"] == 0 and proj["coverage"]["total_lines"] == 0
    assert proj["events"] == []                              # zero admitted, but a real report, not "clear"


def test_body_exactly_at_cap_is_accepted():
    data = b"x" * ROUTE_MAX_BYTES                            # exactly the cap
    s = _stream(data, chunk=1 << 20)
    status, _, _ = import_response(content_type=OCTET, content_length=ROUTE_MAX_BYTES, read_body=s.read)
    assert status == 200


def test_non_conformant_stream_returning_more_than_requested_is_invalid_body():
    status, body, _ = import_response(content_type=OCTET, content_length=8,
                                      read_body=lambda k: b"x" * (k + 5))
    assert status == 400 and json.loads(body) == {"error": "invalid-body"}


def test_no_supplied_value_appears_in_an_error_body():
    marker = b"SEEKRIT-marker-value"
    status, body, _ = import_response(content_type="text/plain", content_length=len(marker), read_body=_no_read)
    assert status == 415 and marker.decode() not in body.decode()


# ── isolated Flask client: STAND-IN auth/CSRF wrapper (static wiring), global 256KiB cap SET ──────────

def _isolated_app():
    from flask import Flask, Response, abort, request

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = GLOBAL_CAP           # mimic the real app; the route must bypass this
    calls = {"auth": 0, "csrf": 0}

    def stand_in_auth(fn):
        def w(*a, **k):
            calls["auth"] += 1
            return fn(*a, **k)
        w.__name__ = fn.__name__
        return w

    def stand_in_csrf(fn):
        def w(*a, **k):
            calls["csrf"] += 1
            if request.headers.get("X-CSRF-Token") != "tok":
                abort(403)
            return fn(*a, **k)
        w.__name__ = fn.__name__
        return w

    @app.route("/api/incidents/import", methods=["POST"])
    @stand_in_auth
    @stand_in_csrf
    def _imp():
        status, body, headers = import_response(
            content_type=request.content_type,
            content_length=request.content_length,
            read_body=lambda limit: request.environ["wsgi.input"].read(limit),
        )
        return Response(body, status=status, headers=headers)

    return app, calls


def test_route_accepts_body_over_the_global_cap_and_under_2mib():
    # a body > 256KiB (the global cap) and <= 2MiB must reach the route (200): proof the raw wsgi.input read
    # bypasses the global MAX_CONTENT_LENGTH. If the adapter used request.get_data(), this would 413.
    app, calls = _isolated_app()
    data = _supported_line() * 4000
    assert GLOBAL_CAP < len(data) <= ROUTE_MAX_BYTES
    r = app.test_client().post("/api/incidents/import", data=data, content_type=OCTET,
                               headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 200 and calls["auth"] == 1 and calls["csrf"] == 1
    assert r.headers["Cache-Control"] == "no-store"
    assert r.get_data() == export_bytes(import_incidents(data))


def test_route_body_over_2mib_is_the_routes_own_413():
    app, _ = _isolated_app()
    big = _supported_line() * 30000                         # > 2 MiB
    assert len(big) > ROUTE_MAX_BYTES
    r = app.test_client().post("/api/incidents/import", data=big, content_type=OCTET,
                               headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 413 and json.loads(r.get_data()) == {"error": "payload-too-large"}


def test_route_missing_csrf_is_403_via_standin_wrapper():
    app, _ = _isolated_app()
    r = app.test_client().post("/api/incidents/import", data=_supported_line(), content_type=OCTET)
    assert r.status_code == 403


def test_route_unsupported_content_type_is_415():
    app, _ = _isolated_app()
    r = app.test_client().post("/api/incidents/import", data=b"hello", content_type="text/plain",
                               headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 415 and json.loads(r.get_data()) == {"error": "unsupported-content-type"}


# ── API-5: strict raw CONTENT_LENGTH parsing (the adapter must not lose invalid/negative declarations) ──

@pytest.mark.parametrize("raw,expected", [
    (None, None), ("", None), ("-1", None), ("1.5", None), (" 10", None), ("10 ", None), ("0x10", None),
    ("+10", None), ("abc", None), (b"10", None),                       # non-str / malformed -> None -> 411
    ("0", 0), ("10", 10), (str(ROUTE_MAX_BYTES), ROUTE_MAX_BYTES),
    (str(ROUTE_MAX_BYTES + 1), ROUTE_MAX_BYTES + 1),                   # over-cap parses; the handler 413s it
])
def test_validated_content_length_is_strict(raw, expected):
    assert validated_content_length(raw) == expected


# The app.py adapter is validated_content_length(request.environ.get("CONTENT_LENGTH")) -> import_response(...).
# These compose the two REAL functions (the request.environ.get + Response glue in app.py is static-reviewed):
# a lossy framework-normalized 0 for an invalid/negative declaration is exactly what this avoids.

@pytest.mark.parametrize("raw", ["-1", "abc", "", None, " 5", "5.0"])
def test_adapter_composition_invalid_declaration_is_411_not_empty(raw):
    cl = validated_content_length(raw)
    status, body, _ = import_response(content_type=OCTET, content_length=cl, read_body=_no_read)
    assert status == 411 and json.loads(body) == {"error": "length-required"}


def test_adapter_composition_valid_zero_is_empty_report_not_411():
    cl = validated_content_length("0")
    status, body, _ = import_response(content_type=OCTET, content_length=cl, read_body=_no_read)
    assert status == 200 and json.loads(body)["coverage"]["total_lines"] == 0


def test_adapter_composition_valid_length_reads_and_reports():
    data = _supported_line()
    cl = validated_content_length(str(len(data)))
    status, body, _ = import_response(content_type=OCTET, content_length=cl, read_body=_stream(data).read)
    assert status == 200 and body == export_bytes(import_incidents(data))


# ── API-3: bound the decimal length BEFORE int() so a long digit string can't trip the conversion limit ──

@pytest.mark.parametrize("raw,expected", [
    ("007", 7), ("0000000010", 10), ("00", 0), ("0000000", 0),        # leading zeros normalized (no int() on 0-runs)
    (str(ROUTE_MAX_BYTES), ROUTE_MAX_BYTES),                          # exactly at the cap is accepted
    ("0" + str(ROUTE_MAX_BYTES), ROUTE_MAX_BYTES),                    # at the cap with a leading zero
])
def test_validated_content_length_normalizes_leading_zeros(raw, expected):
    assert validated_content_length(raw) == expected


@pytest.mark.parametrize("raw", [str(ROUTE_MAX_BYTES + 1), "9999999", "10000000", "999999999999",
                                 "00000009999999"])
def test_over_cap_declaration_saturates_and_never_converts(raw):
    # An over-cap declaration takes the saturation branch (returns cap+1) BEFORE int(), so int() is reached only
    # for a value within the cap's digit count and the interpreter's integer string-conversion limit is never
    # approached. Values whose TRUE integer differs from cap+1 (e.g. 9999999, 10000000) prove the saturation
    # branch is taken rather than a bare int(raw) -- a regression to int(raw) would return the true value here.
    assert validated_content_length(raw) == ROUTE_MAX_BYTES + 1


def test_over_cap_declaration_composes_to_a_fixed_413():
    cl = validated_content_length("9999999")                          # a valid, over-cap declaration
    status, body, _ = import_response(content_type=OCTET, content_length=cl, read_body=_no_read)
    assert status == 413 and json.loads(body) == {"error": "payload-too-large"}


# ── API-6: header-only CSRF on the raw-body route rejects a missing/invalid header WITHOUT reading the body ──
# csrf_valid is the REAL mechanism (from src.security.web_auth). The header-only decorator + route below
# reconstruct app.py's requires_csrf_header composition; the ACTUAL app.py closure is static-reviewed (not
# importable without the app factory), so this proves the boundary's principle with the real token check, not
# the exact production closure.

def test_header_only_csrf_rejects_missing_or_invalid_header_without_reading_body():
    from flask import Flask, Response, abort, request, session
    from src.security.web_auth import csrf_valid

    app = Flask(__name__)
    app.secret_key = "test-only-secret"
    app.config["MAX_CONTENT_LENGTH"] = GLOBAL_CAP
    reads = {"n": 0}

    def requires_csrf_header(fn):
        def w(*a, **k):
            if not csrf_valid(session.get("csrf"), request.headers.get("X-CSRF-Token")):
                abort(403)
            return fn(*a, **k)
        w.__name__ = fn.__name__
        return w

    @app.route("/imp", methods=["POST"])
    @requires_csrf_header
    def _imp():
        def read_body(limit):
            reads["n"] += 1
            return request.environ["wsgi.input"].read(limit)
        status, body, headers = import_response(
            content_type=request.content_type,
            content_length=validated_content_length(request.environ.get("CONTENT_LENGTH")),
            read_body=read_body)
        return Response(body, status=status, headers=headers)

    c = app.test_client()
    with c.session_transaction() as s:
        s["csrf"] = "tok"
    data = _supported_line()

    r = c.post("/imp", data=data, content_type=OCTET)                          # missing header
    assert r.status_code == 403 and reads["n"] == 0                            # body NEVER read
    r = c.post("/imp", data=data, content_type=OCTET, headers={"X-CSRF-Token": "nope"})
    assert r.status_code == 403 and reads["n"] == 0                            # wrong header -> still no read
    r = c.post("/imp", data=data, content_type=OCTET, headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 200 and reads["n"] >= 1                            # valid header -> route reads
    assert r.get_data() == export_bytes(import_incidents(data))
