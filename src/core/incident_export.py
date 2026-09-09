"""Default-redacted, self-contained export of an :class:`~src.core.incident_import.IncidentReport`.

Export uses its OWN fixed-field projection — never the raw file or an on-screen source view. Throughout the
complete exported bytes it omits original file names/paths, addresses, coordinates, node/source identifiers,
raw strings, and arbitrary ``type`` text: a known category exports as its fixed CC label, an unknown category
as ``unsupported`` (position + fixed reason only). Coverage and clock uncertainty are preserved so a redacted
export still cannot be mistaken for an all-clear, and the bytes are self-contained (no external resources or
executable links). The export preview and the serialized output share the exact same projection.
"""
from __future__ import annotations

import json
from typing import Any, Dict

from src.core.incident_import import IncidentReport

EXPORT_FORMAT = "cc-incident-report-v1"


def export_projection(report: IncidentReport) -> Dict[str, Any]:
    """The independent fixed-field projection shared by the preview and the serialized bytes. Retains only the
    report identity (file digest), coverage counts, clock uncertainty, admitted events (position + fixed label
    + claimed clock), and unsupported/malformed positions with a fixed reason — nothing raw or identifying."""
    return {
        "format": EXPORT_FORMAT,
        "report_identity": {"file_sha256": report.file_sha256, "file_bytes": report.file_bytes},
        "coverage": {
            **report.counts,
            "truncated": report.truncated,
            "truncation_reason": report.truncation_reason,
            "final_line_without_newline": report.final_line_without_newline,
        },
        "clock_uncertainty": dict(report.clock_uncertainty),
        "events": [{"line": e.line, "category": e.category, "ts": e.ts, "epoch": e.epoch}
                   for e in report.events],
        "unsupported": [{"line": u["line"], "reason": u["reason"]} for u in report.unsupported],
        "malformed": [{"line": m["line"], "reason": m["reason"]} for m in report.malformed],
    }


def export_preview(report: IncidentReport) -> Dict[str, Any]:
    """The on-export preview object — identical to the serialized projection (same fixed-field structure)."""
    return export_projection(report)


def export_bytes(report: IncidentReport) -> bytes:
    """Serialize the projection to self-contained JSON bytes. ``ensure_ascii`` keeps the output portable; the
    projection carries no external resource, URL, or executable link."""
    return json.dumps(export_projection(report), ensure_ascii=True, sort_keys=True, indent=2).encode("utf-8")
