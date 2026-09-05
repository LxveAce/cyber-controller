"""Rayhunter monitor Phase 1 — bounded NDJSON analysis-report parser + read-only snapshot.

Covers the plan's acceptance cases with deterministic fixtures (no hardware): counts by level, malformed
/ oversized / split lines, byte + event budgets, unknown severity, missing timestamp, and the address /
path / redirect / size guards on the fetcher."""

from __future__ import annotations

import json

from src.core import rayhunter_monitor as rm


def _ndjson(*objs) -> bytes:
    return ("\n".join(json.dumps(o) for o in objs)).encode("utf-8")


# Tagged v0.12.0 schema: metadata carries analyzers (positional) + report_version; rows carry
# packet_timestamp + a positional events list + singular skipped_message_reason.
META = {"report_version": 2, "analyzers": [{"name": "imsi", "version": 1}, {"name": "cell", "version": 1}]}


def test_counts_by_level_and_warnings():
    data = _ndjson(
        META,
        {"packet_timestamp": "2023-01-01T00:00:00+00:00",
         "events": [{"event_type": "High", "message": "x"}, None]},
        {"packet_timestamp": "2023-01-01T00:00:01+00:00",
         "events": [None, {"event_type": "Low", "message": "y"}]},
        {"packet_timestamp": "2023-01-01T00:00:02+00:00",
         "events": [{"event_type": "Informational", "message": "i"}, None]},
        {"skipped_message_reason": "unsupported"},
    )
    out = rm.parse_analysis_report(data)
    assert out["metadata"]["report_version"] == 2
    assert out["analyzers"] == ["imsi", "cell"]
    c = out["counts"]
    assert c["by_level"]["High"] == 1 and c["by_level"]["Low"] == 1
    assert c["warnings"] == 2 and c["informational"] == 1 and c["skipped"] == 1
    assert out["complete"] is True and out["coverage"] == "complete"
    # positional analyzer mapping: the High event is analyzer[0]=imsi, the Low is analyzer[1]=cell
    highs = [e for e in out["events"] if e["level"] == "High"]
    lows = [e for e in out["events"] if e["level"] == "Low"]
    assert highs[0]["analyzer"] == "imsi" and lows[0]["analyzer"] == "cell"


def test_malformed_lines_flag_incomplete():
    data = b"not json at all\n" + json.dumps(META).encode() + b"\n" + b'{"broken": \n' + \
        json.dumps({"events": [{"event_type": "Medium"}]}).encode() + b"\n\n"
    out = rm.parse_analysis_report(data)
    assert out["malformed_lines"] >= 2
    assert out["counts"]["by_level"]["Medium"] == 1
    assert out["complete"] is False                 # a malformed line IS a loss of coverage
    assert "malformed-line" in out["coverage"]


def test_invalid_utf8_line_is_flagged_not_raised():
    data = json.dumps(META).encode() + b"\n\xff\xfe\x00\n"
    out = rm.parse_analysis_report(data)
    assert out["malformed_lines"] == 1 and out["complete"] is False


def test_oversized_line_flags_incomplete():
    big = {"events": [{"event_type": "High", "message": "z" * 5000}]}
    data = _ndjson(META, big)
    out = rm.parse_analysis_report(data, max_line_bytes=1000)
    assert out["oversized_lines"] == 1 and out["metadata"] is not None
    assert out["counts"]["warnings"] == 0
    assert out["complete"] is False and "oversized-line" in out["coverage"]


def test_byte_budget_marks_incomplete():
    rows = [{"packet_timestamp": str(i), "events": [{"event_type": "Low"}]} for i in range(50)]
    out = rm.parse_analysis_report(_ndjson(META, *rows), max_bytes=120)
    assert out["complete"] is False and "truncated-bytes" in out["coverage"]


def test_event_budget_marks_incomplete():
    rows = [{"packet_timestamp": str(i), "events": [{"event_type": "Low"}]} for i in range(20)]
    out = rm.parse_analysis_report(_ndjson(META, *rows), max_events=5)
    assert out["complete"] is False and "truncated-events" in out["coverage"]
    assert out["counts"]["events"] == 5


def test_unknown_severity_flagged_not_miscounted():
    out = rm.parse_analysis_report(_ndjson(META, {"events": [{"event_type": "Critical", "message": "?"}]}))
    assert all(v == 0 for v in out["counts"]["by_level"].values())
    assert out["counts"]["events"] == 1 and out["events"][0]["level"] == "Critical"
    assert out["complete"] is False and "unknown-severity" in out["coverage"]


def test_unsupported_report_version_flagged():
    bad_meta = {"report_version": 99, "analyzers": []}
    out = rm.parse_analysis_report(_ndjson(bad_meta, {"packet_timestamp": "t", "events": []}))
    assert out["complete"] is False and "unsupported-report-version" in out["coverage"]


def test_missing_metadata_is_incomplete():
    # rows only, no ReportMetadata line
    out = rm.parse_analysis_report(_ndjson({"packet_timestamp": "t", "events": [{"event_type": "Low"}]}))
    assert out["complete"] is False and "missing-metadata" in out["coverage"]


def test_scalar_event_is_malformed_not_a_null_slot():
    out = rm.parse_analysis_report(_ndjson(META, {"packet_timestamp": "t", "events": [42]}))
    assert out["complete"] is False and "malformed-event" in out["coverage"]
    assert out["counts"]["warnings"] == 0


def test_empty_event_type_object_flagged():
    out = rm.parse_analysis_report(_ndjson(META, {"packet_timestamp": "t", "events": [{"event_type": {}}]}))
    assert out["complete"] is False and "unknown-severity" in out["coverage"]


def test_null_positional_slot_is_allowed():
    # events: [null, High] — the null is a legit slot (analyzer[0] produced nothing); NOT a loss
    out = rm.parse_analysis_report(
        _ndjson(META, {"packet_timestamp": "t", "events": [None, {"event_type": "High", "message": "x"}]}))
    assert out["complete"] is True and out["coverage"] == "complete"
    assert out["counts"]["by_level"]["High"] == 1
    assert out["events"][0]["analyzer"] == "cell"   # positional: index 1 -> analyzers[1]=cell


def test_metadata_without_report_version_is_incomplete():
    # N03: analyzers present but no report_version — we can't confirm the v2 contract, so NOT complete.
    meta = {"analyzers": [{"name": "imsi", "version": 1}]}
    out = rm.parse_analysis_report(_ndjson(meta, {"packet_timestamp": "t", "events": []}))
    assert out["complete"] is False and "missing-report-version" in out["coverage"]


def test_metadata_with_non_list_analyzers_is_incomplete():
    # N03: analyzers:42 can't back the positional event->analyzer mapping — malformed metadata.
    meta = {"report_version": 2, "analyzers": 42}
    out = rm.parse_analysis_report(_ndjson(meta, {"packet_timestamp": "t", "events": [{"event_type": "Low"}]}))
    assert out["complete"] is False and "malformed-metadata" in out["coverage"]


def test_row_missing_events_is_malformed_not_complete():
    # N03: a non-skipped AnalysisRow with no events list is malformed — not a silent zero-warning row.
    out = rm.parse_analysis_report(_ndjson(META, {"packet_timestamp": "t"}))
    assert out["complete"] is False and "malformed-row" in out["coverage"]


def test_row_with_scalar_events_is_malformed_not_complete():
    # N03: events:42 (a scalar where the events LIST belongs) is malformed, never skipped as complete.
    out = rm.parse_analysis_report(_ndjson(META, {"packet_timestamp": "t", "events": 42}))
    assert out["complete"] is False and "malformed-row" in out["coverage"]


def test_empty_events_list_stays_complete():
    # A row that genuinely produced no events (events: []) is valid and complete — not a malformed row.
    out = rm.parse_analysis_report(_ndjson(META, {"packet_timestamp": "t", "events": []}))
    assert out["complete"] is True and out["coverage"] == "complete"


def test_n03_export_propagates_incompleteness():
    # The incompleteness must reach the export payload, not just the parser dict.
    parsed = rm.parse_analysis_report(_ndjson({"analyzers": []}, {"packet_timestamp": "t", "events": 42}))
    snap = rm.build_snapshot({"runtime_metadata": {"rayhunter_version": "0.12.0"}}, {}, {})
    export = rm.build_report_export(snap, parsed, cc_version="test", exported_at="2026-09-04T00:00:00Z")
    assert export["json"]["complete"] is False
    assert "Incomplete coverage" in export["html"]


def test_missing_timestamp_stays_unknown():
    out = rm.parse_analysis_report(_ndjson(META, {"events": [{"event_type": "Low"}]}))
    assert out["events"][0]["timestamp"] is None   # never coerced to epoch


# -- snapshot ---------------------------------------------------------

def test_build_snapshot_none_inputs_are_unknown_not_healthy():
    snap = rm.build_snapshot(None, None, None)
    assert snap["transport"] == "unreachable"
    assert snap["recording"] == "unknown"
    assert snap["version"] is None
    assert "does not prove useful capture" in snap["note"]


def test_build_snapshot_recording_and_reanalysis():
    stats = {"runtime_metadata": {"rayhunter_version": "0.12.0"}, "battery_status": {"level": 100}}
    manifest = {"current_entry": {"name": "rec-1"}, "entries": []}
    analysis = {"running": "rec-1", "queued": [], "finished": []}
    snap = rm.build_snapshot(stats, manifest, analysis)
    assert snap["transport"] == "ok" and snap["version"] == "0.12.0"
    assert snap["recording"] == "recording" and snap["recording_name"] == "rec-1"
    assert snap["reanalysis_running"] == "rec-1"


def test_build_snapshot_stopped_recording():
    snap = rm.build_snapshot({"runtime_metadata": {}}, {"current_entry": None, "entries": []}, {"running": None})
    assert snap["recording"] == "stopped"


# -- fetch_json guards ------------------------------------------------

class _Raw:
    def __init__(self, data):
        self._data = data

    def read(self, n, decode_content=True):
        return self._data[:n]


class _Resp:
    def __init__(self, status_code, body=b"{}"):
        self.status_code = status_code
        self.raw = _Raw(body)

    def close(self):
        pass


def test_fetch_json_rejects_non_local_or_malformed_ip():
    called = {"n": 0}

    def getter(*a, **k):
        called["n"] += 1
        return _Resp(200)

    for bad in ("8.8.8.8", "evil.example.com", "010.0.0.1", "192.168.1.1'"):
        assert rm.fetch_json(bad, "/api/system-stats", getter=getter) is None
    assert called["n"] == 0   # never even issued a request for a bad address


def test_fetch_json_rejects_unknown_path():
    def getter(*a, **k):
        raise AssertionError("must not request a non-allowlisted path")

    assert rm.fetch_json("192.168.1.1", "/api/../secret", getter=getter) is None
    assert rm.fetch_json("192.168.1.1", "/api/system-stats/../analysis", getter=getter) is None


def test_fetch_json_no_redirect_and_200_only():
    seen = {}

    def getter(url, **kw):
        seen["allow_redirects"] = kw.get("allow_redirects")
        return _Resp(302)

    assert rm.fetch_json("192.168.1.1", "/api/system-stats", getter=getter) is None
    assert seen["allow_redirects"] is False


def test_fetch_json_happy_path():
    payload = {"runtime_metadata": {"rayhunter_version": "0.12.0"}}

    def getter(url, **kw):
        return _Resp(200, json.dumps(payload).encode("utf-8"))

    got = rm.fetch_json("192.168.1.1", "/api/system-stats", getter=getter)
    assert got == payload


def test_fetch_json_size_bounded():
    def getter(url, **kw):
        return _Resp(200, b"x" * 5000)

    assert rm.fetch_json("192.168.1.1", "/api/system-stats", getter=getter, max_bytes=100) is None


def test_snapshot_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")
    from src.ui.web.app import create_app
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.security.web_auth import new_csrf_token

    # stub the device reads so no real network is touched
    def fake_fetch(admin_ip, path, **kw):
        return {"runtime_metadata": {"rayhunter_version": "0.12.0"}} if path == "/api/system-stats" else \
               ({"current_entry": {"name": "rec-1"}} if path == "/api/qmdl-manifest" else {"running": None})
    monkeypatch.setattr(rm, "fetch_json", fake_fetch)

    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool())
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["cred_gen"] = client.application.extensions["cc_web_credentials"].generation
        sess["csrf"] = new_csrf_token()
        csrf = sess["csrf"]
    # malformed IP rejected at the boundary
    assert client.get("/api/rayhunter/snapshot?admin_ip=010.0.0.1",
                      headers={"X-CSRF-Token": csrf}).status_code == 400
    r = client.get("/api/rayhunter/snapshot?admin_ip=192.168.1.1", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    j = r.get_json()
    assert j["transport"] == "ok" and j["version"] == "0.12.0" and j["recording"] == "recording"


# -- Phase 2: report name validation, fetch, redacted/escaped export --

def test_valid_report_name():
    assert rm._valid_report_name("rec-2026_08_03.qmdl")
    for bad in ("", "..", "a/b", "../secret", "a\x00b", "rec;rm", "x" * 200, "a b"):
        assert not rm._valid_report_name(bad), bad


def test_name_in_manifest():
    manifest = {"entries": [{"name": "rec-1"}, {"name": "rec-2"}], "current_entry": {"name": "rec-3"}}
    assert rm._name_in_manifest("rec-1", manifest)
    assert rm._name_in_manifest("rec-3", manifest)   # current entry counts
    assert not rm._name_in_manifest("rec-9", manifest)


def test_fetch_report_rejects_bad_name_before_request():
    def getter(*a, **k):
        raise AssertionError("must not request for an invalid/unknown report name")

    assert rm.fetch_report("192.168.1.1", "../etc/passwd", getter=getter) is None
    manifest = {"entries": [{"name": "known"}]}
    assert rm.fetch_report("192.168.1.1", "unknown", manifest=manifest, getter=getter) is None


def test_fetch_report_happy_path():
    ndjson = (json.dumps(META) + "\n" +
              json.dumps({"events": [{"event_type": "High", "message": "x"}]})).encode("utf-8")

    def getter(url, **kw):
        assert url.endswith("/api/analysis-report/known")
        return _Resp(200, ndjson)

    out = rm.fetch_report("192.168.1.1", "known", manifest={"entries": [{"name": "known"}]}, getter=getter)
    assert out is not None and out["counts"]["warnings"] == 1


def test_fetch_report_oversized_flags_incomplete():
    """#9: an oversized report must NOT read as complete/zero-warnings — the overflow reaches the parser."""
    big_row = json.dumps({"packet_timestamp": "t", "events": [{"event_type": "High", "message": "z"}]})
    ndjson = (json.dumps(META) + "\n" + big_row).encode("utf-8")

    def getter(url, **kw):
        return _Resp(200, ndjson)

    out = rm.fetch_report("192.168.1.1", "known", manifest={"entries": [{"name": "known"}]},
                          getter=getter, max_bytes=len(json.dumps(META)) + 5)
    assert out is not None
    assert out["complete"] is False and "truncated-bytes" in out["coverage"]


def test_build_snapshot_ignores_non_dict_device_reply():
    """#8: a non-dict endpoint reply must not crash build_snapshot; it reads as unreachable/unknown."""
    snap = rm.build_snapshot([1, 2, 3], "not-a-dict", 42)
    assert snap["transport"] == "unreachable" and snap["recording"] == "unknown"


def _sample_parsed(msg="secret-imsi-12345"):
    data = _ndjson(META, {"packet_timestamp": "2023-01-01T00:00:00+00:00",
                          "events": [{"event_type": "High", "message": msg}]})
    return rm.parse_analysis_report(data)


def test_export_redacts_messages_by_default():
    parsed = _sample_parsed("secret-imsi-12345")
    snap = rm.build_snapshot({"runtime_metadata": {"rayhunter_version": "0.12.0"}}, None, None)
    exp = rm.build_report_export(snap, parsed, cc_version="2.0.0", exported_at="2026-09-04T00:00:00Z")
    ev = exp["json"]["events"][0]
    assert "message" not in ev and ev["message_redacted"] is True    # raw message NOT exported
    assert "secret-imsi-12345" not in exp["json"]["disclaimer"]
    assert "secret-imsi-12345" not in exp["html"]                    # and not in the HTML
    assert exp["json"]["detailed_evidence_included"] is False


def test_export_detailed_includes_message():
    parsed = _sample_parsed("evidence-string")
    snap = rm.build_snapshot({"runtime_metadata": {}}, None, None)
    exp = rm.build_report_export(snap, parsed, cc_version="2.0.0", exported_at="t", include_detailed=True)
    assert exp["json"]["events"][0]["message"] == "evidence-string"
    assert exp["json"]["detailed_evidence_included"] is True


def test_export_escapes_hostile_html():
    parsed = _sample_parsed("<img src=x onerror=alert(1)>")
    snap = rm.build_snapshot({"runtime_metadata": {}}, None, None)
    exp = rm.build_report_export(snap, parsed, cc_version="2.0.0", exported_at="t", include_detailed=True)
    assert "<img src=x" not in exp["html"]           # the raw tag must be escaped
    assert "&lt;img" in exp["html"]


def test_export_digest_reproducible_and_disclaimer_present():
    parsed = _sample_parsed()
    snap = rm.build_snapshot({"runtime_metadata": {}}, None, None)
    a1 = rm.build_report_export(snap, parsed, cc_version="2.0.0", exported_at="fixed")
    a2 = rm.build_report_export(snap, parsed, cc_version="2.0.0", exported_at="fixed")
    assert a1["digest"] == a2["digest"] and len(a1["digest"]) == 64
    assert "NOT proof of an IMSI catcher" in a1["json"]["disclaimer"]


def test_export_flags_incomplete_coverage():
    parsed = rm.parse_analysis_report(
        _ndjson(META, *[{"events": [{"event_type": "Low"}]} for _ in range(10)]), max_events=3)
    snap = rm.build_snapshot({"runtime_metadata": {}}, None, None)
    exp = rm.build_report_export(snap, parsed, cc_version="2.0.0", exported_at="t")
    assert exp["json"]["complete"] is False
    assert "Incomplete coverage" in exp["html"]
