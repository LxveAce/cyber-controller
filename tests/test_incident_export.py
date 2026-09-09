"""Tests for the default-redacted incident export. Synthetic report only. Proves the COMPLETE exported bytes
carry no filename/address/coordinate/node/source/raw/arbitrary-type token, that preview == serialized, that
coverage/uncertainty are retained, and that the bytes are self-contained.
"""
from __future__ import annotations

import json

from src.core.incident_export import EXPORT_FORMAT, export_bytes, export_preview, export_projection
from src.core.incident_import import import_incidents


def _report_with_sensitive_inputs():
    # A fixture whose rows carry sensitive/identifying values in raw/node/src and an unknown type string, plus
    # supported rows. None of these must survive into the export.
    data = (
        b'{"ts":10,"epoch":1700000000,"node":"!SECRETNODE","src":"esp32-DEADBEEF","type":"EVILTWIN",'
        b'"raw":"ssid=HomeNet bssid=AA:BB:CC:DD:EE:FF lat=40.7128 lon=-74.0060 /sd/incidents/run7.jsonl"}\n'
        b'{"ts":20,"epoch":1700000001,"node":"!N2","src":"s","type":"SUPER_SECRET_FUTURE_TYPE","raw":"x"}\n'
        b'{"ts":30,"epoch":1700000002,"node":"!N3","src":"s","type":"DEAUTH_FLOOD","raw":"MAC=11:22:33:44:55:66"}\n'
        b'garbage-line\n'
    )
    return import_incidents(data)


_FORBIDDEN_TOKENS = [
    "secretnode", "deadbeef", "super_secret_future_type", "homenet", "aa:bb:cc", "11:22:33",
    "40.7128", "-74.0060", "lat=", "lon=", "/sd/", "incidents/run7", "ssid=", "bssid=", "raw", "node", "src",
]


def test_complete_export_bytes_contain_no_sensitive_or_identifying_token():
    blob = export_bytes(_report_with_sensitive_inputs()).decode("utf-8").lower()
    for token in _FORBIDDEN_TOKENS:
        assert token not in blob, f"export leaked {token!r}"


def test_events_project_only_line_category_and_clock():
    proj = export_projection(_report_with_sensitive_inputs())
    for ev in proj["events"]:
        assert set(ev) == {"line", "category", "ts", "epoch"}
        assert ev["category"] in {"EVILTWIN", "DEAUTH_FLOOD"}     # fixed CC labels only
    # the unsupported entry is position + fixed reason, no type text
    assert proj["unsupported"] == [{"line": 2, "reason": "unsupported-type"}]


def test_preview_equals_serialized_projection():
    rep = _report_with_sensitive_inputs()
    assert export_preview(rep) == json.loads(export_bytes(rep))


def test_coverage_and_clock_uncertainty_are_retained():
    proj = export_projection(_report_with_sensitive_inputs())
    assert proj["coverage"]["admitted"] == 2 and proj["coverage"]["unsupported"] == 1
    assert proj["coverage"]["malformed"] == 1 and proj["coverage"]["total_lines"] == 4
    assert proj["clock_uncertainty"]["global_chronology"] == "unsupported"
    assert proj["report_identity"]["file_sha256"] == rep_sha(_report_with_sensitive_inputs())


def rep_sha(report):
    return report.file_sha256


def test_export_is_self_contained_valid_json_with_no_external_links():
    blob = export_bytes(_report_with_sensitive_inputs()).decode("utf-8")
    json.loads(blob)                                              # valid, self-contained JSON
    for scheme in ("http://", "https://", "file://", "://", "<script", "javascript:"):
        assert scheme not in blob.lower()
    assert json.loads(blob)["format"] == EXPORT_FORMAT


def test_empty_input_exports_a_no_all_clear_report():
    proj = export_projection(import_incidents(b""))
    assert proj["coverage"]["admitted"] == 0 and proj["coverage"]["total_lines"] == 0
    assert proj["events"] == []                                  # zero admitted, but a real report, not "clear"
