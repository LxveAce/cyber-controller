"""Bounded, memory-only reader for an AntiHunter SD ``incidents.jsonl`` file (offline incident workspace core).

This reads a caller-selected file's BYTES and produces a normalized, privacy-safe report. Each physical line
must be exactly the accepted six-field record — ``ts``/``epoch``/``node``/``src``/``type``/``raw`` and nothing
else — with ``ts``/``epoch`` unsigned 32-bit integers (booleans excluded) and ``node``/``src``/``type``/``raw``
strings. The full record schema is validated BEFORE the category is classified: a missing field, an extra
field, a non-string text value, or a non-integer/out-of-range clock makes the whole line malformed with a
fixed reason that carries no input value — never admitted with a nulled clock, and never mislabelled
unsupported. Only a well-formed six-field record whose ``type`` is not a recognized CC label is ``unsupported``.
An admitted record keeps only a fixed CC label, its physical-line position, and the two validated clock
fields; it retains **no** raw free text, node/source identifiers, or extra fields, so nothing sensitive can
flow into a search index, an error, or the default export.

Format: the inspected AntiHunter SD ``incidents.jsonl`` producer only — one JSON object per physical line with
the six fields above (``node`` is a quoted string, not a numeric id). This is an independent reader written to
the observed shape; no upstream code is copied or translated, and recognizing the shape does not authenticate
the producer — an unknown firmware/variant stays unknown.

It parses nothing outside these limits and never repairs or heuristically joins physical lines: the upstream's
incomplete control-character escaping can break a line, so a broken line is marked malformed, never stitched
into a trusted event. Blank, malformed, unsupported, and admitted lines are counted separately; a partial or
empty input is never an all-clear. Separate clock domains (relative uptime vs claimed RTC epoch vs a peer's
local-logger receipt) are carried with their uncertainty and never merged into a global chronology.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Bounds validated BEFORE any expensive parse/store.
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_LINE_BYTES = 16 * 1024
MAX_LINES = 10_000

# The presentation allowlist for this first slice: imported observation LABELS, not detectors or commands.
# A proper subset of the producer's full type set; a well-formed record with any other type is reported
# unsupported, never dropped and never admitted.
SUPPORTED_TYPES = frozenset({
    "EVILTWIN", "BEACON_FORGE", "DEAUTH_FORGE", "DEAUTH_FLOOD", "PROBE_FLOOD", "CSA_SPOOF", "SSID_CONFUSION",
})

# The accepted record schema: EXACTLY these six fields (ts, epoch, node, src, type, raw), with ts/epoch
# unsigned 32-bit integers and the other four strings. The boundary is checked before any category decision.
REQUIRED_FIELDS = frozenset({"ts", "epoch", "node", "src", "type", "raw"})
_TEXT_FIELDS = ("node", "src", "type", "raw")
_CLOCK_FIELDS = ("ts", "epoch")

# Claimed-clock uncertainty is fixed and honest: nothing here proves a global event ordering.
CLOCK_UNCERTAINTY = {
    "global_chronology": "unsupported",
    "ts": "relative uptime; no boot identifier, cannot order across reboots",
    "epoch": "claimed RTC epoch; presence is not accuracy",
    "peer": "peer rows carry the local logger's receipt time, a separate domain",
}


@dataclass
class IncidentEvent:
    """One admitted supported observation. Carries only the fixed label, position, and the two validated clock
    fields — never raw text, node/source ids, addresses, coordinates, or any extra field."""

    line: int                       # 1-based physical line position = distinct event identity
    category: str                   # a fixed CC label from SUPPORTED_TYPES
    ts: int                         # claimed relative uptime, validated unsigned 32-bit
    epoch: int                      # claimed RTC epoch, validated unsigned 32-bit


@dataclass
class IncidentReport:
    file_sha256: str                # digest of the exact file bytes = report identity
    file_bytes: int
    truncated: bool = False
    truncation_reason: Optional[str] = None
    final_line_without_newline: bool = False
    events: List[IncidentEvent] = field(default_factory=list)
    unsupported: List[Dict[str, Any]] = field(default_factory=list)   # {line, reason} — position + fixed reason
    malformed: List[Dict[str, Any]] = field(default_factory=list)     # {line, reason} — position + fixed reason
    counts: Dict[str, int] = field(default_factory=dict)              # admitted/blank/malformed/unsupported/total
    clock_uncertainty: Dict[str, str] = field(default_factory=lambda: dict(CLOCK_UNCERTAINTY))


def _schema_error(obj: Dict[str, Any]) -> Optional[str]:
    """Return a fixed reason if *obj* is not exactly the accepted six-field record with the right scalar
    domains, else ``None``. The reason names the failing rule only and carries no input value.

    ``type(x) is int`` excludes ``bool`` (``type(True)`` is ``bool``) and every JSON float, so a boolean or a
    fractional/infinite clock is rejected as an invalid clock rather than silently coerced. Because the type
    check is the first operand of the ``or``, an out-of-domain comparison never runs on a non-integer value."""
    if obj.keys() != REQUIRED_FIELDS:
        return "invalid-field-set"
    if any(type(obj[key]) is not str for key in _TEXT_FIELDS):
        return "invalid-string-field"
    if any(type(obj[key]) is not int or not 0 <= obj[key] <= 0xFFFFFFFF for key in _CLOCK_FIELDS):
        return "invalid-clock-field"
    return None


def import_incidents(data: bytes) -> IncidentReport:
    """Parse *data* (the caller-selected file's exact bytes) into a normalized report. Never raises on ordinary
    malformed content; over-limit inputs are recorded, not partially parsed."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("import_incidents expects the file bytes")
    data = bytes(data)
    report = IncidentReport(file_sha256=hashlib.sha256(data).hexdigest(), file_bytes=len(data))
    counts = {"total_lines": 0, "admitted": 0, "blank": 0, "malformed": 0, "unsupported": 0}

    if len(data) > MAX_FILE_BYTES:
        report.truncated = True
        report.truncation_reason = "file-exceeds-2MiB; not parsed"
        report.counts = counts
        return report

    # Split into PHYSICAL lines on \n; a final chunk with no trailing newline is a physical line too.
    raw_lines = data.split(b"\n")
    report.final_line_without_newline = bool(raw_lines) and raw_lines[-1] != b"" and not data.endswith(b"\n")
    if raw_lines and raw_lines[-1] == b"":
        raw_lines = raw_lines[:-1]   # a trailing newline does not create an empty final record

    if len(raw_lines) > MAX_LINES:
        report.truncated = True
        report.truncation_reason = f"file-exceeds-{MAX_LINES}-lines; parsed the first {MAX_LINES}"
        raw_lines = raw_lines[:MAX_LINES]

    for index, raw in enumerate(raw_lines, start=1):
        counts["total_lines"] += 1
        if len(raw) > MAX_LINE_BYTES:
            counts["malformed"] += 1
            report.malformed.append({"line": index, "reason": "line-exceeds-16KiB"})
            continue
        if raw.strip() == b"":
            counts["blank"] += 1
            continue
        try:
            obj = json.loads(raw)          # never repaired; a broken control-char line just fails here
        except (ValueError, UnicodeDecodeError):
            counts["malformed"] += 1
            report.malformed.append({"line": index, "reason": "invalid-json"})
            continue
        if not isinstance(obj, dict):
            counts["malformed"] += 1
            report.malformed.append({"line": index, "reason": "not-an-object"})
            continue

        # Enforce the accepted six-field schema BEFORE classifying the category. A missing/extra field, a
        # non-string text value, or a non-integer/out-of-range clock is malformed with a fixed reason — never
        # admitted with a nulled clock, and never mislabelled unsupported.
        schema_reason = _schema_error(obj)
        if schema_reason is not None:
            counts["malformed"] += 1
            report.malformed.append({"line": index, "reason": schema_reason})
            continue

        category = obj["type"]                     # a validated string
        if category not in SUPPORTED_TYPES:
            counts["unsupported"] += 1             # well-formed record; position + fixed reason, type text dropped
            report.unsupported.append({"line": index, "reason": "unsupported-type"})
            continue

        # Admitted: keep only the fixed label, position, and the two validated clock fields. node/src/raw and
        # any other content are deliberately dropped and never retained.
        report.events.append(IncidentEvent(line=index, category=category,
                                           ts=obj["ts"], epoch=obj["epoch"]))
        counts["admitted"] += 1

    report.counts = counts
    return report
