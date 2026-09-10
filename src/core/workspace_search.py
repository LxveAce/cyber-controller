"""Deterministic, offline search & filter over already-redacted incident-workspace records.

This is the pure core for the "find my own workspace records" behaviour: given a list of records the caller
has ALREADY produced (the default-redacted rows of an :func:`~src.core.incident_export.export_projection`
result — position + fixed status/label + fixed reason + claimed clock, nothing raw or identifying), it narrows
them by an optional plain-text substring, an optional type/label (``category``), and an optional ``status``.
The three narrowings combine with AND, matching the operator's mental model of the existing status/category
filter while adding the plain-text dimension that the workspace does not yet have anywhere.

What this deliberately does NOT do:

* No I/O of any kind. It never opens a file, never fetches a URL, never imports the parser/export layer. A
  filename or URL that might appear inside a supplied record is INERT text — searched as characters, never
  followed. The input is exactly the records the caller hands in; nothing is discovered, enumerated, or read.
* No new field ever leaks through search. Every supplied record is first projected to a FIXED safe allowlist
  (:data:`_RECORD_FIELDS`); any extra key a caller passes is dropped — never matched against, never returned.
  This re-asserts the export redaction boundary at the search layer, so a stray ``raw``/``node``/``src`` key
  cannot ride through a search result. The plain-text query only ever inspects the textual metadata fields in
  :data:`SEARCHABLE_FIELDS` (status/category/reason) — never the numeric clock/position, and never a dropped
  field.
* No repair, ranking, scoring, or fuzzy expansion. Matching is a plain case-insensitive substring test; the
  result order is a fixed deterministic key (position, then status/category/reason) so the same inputs always
  yield the same bytes. There is no relevance heuristic that could silently reorder or hide a record.

Bounds are validated up front, before any scan: the record count is capped at :data:`MAX_RECORDS` (the parser's
own line cap) and the query text at :data:`MAX_QUERY_CHARS`. An over-bound input is a fixed-code error, never a
silently truncated-and-run search. The vocabularies (:data:`SUPPORTED_TYPES`, :data:`STATUSES`) are redeclared
here as local constants so this module has no import-time dependency that could reach parsing or the filesystem;
they mirror ``incident_import.SUPPORTED_TYPES`` and the three record statuses. Synthetic input only.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# The presentation label allowlist, mirrored from incident_import.SUPPORTED_TYPES. A category filter must name
# one of these (or be an "any" sentinel); an unknown label is a fixed error, never a silent match-nothing.
SUPPORTED_TYPES = frozenset({
    "EVILTWIN", "BEACON_FORGE", "DEAUTH_FORGE", "DEAUTH_FLOOD", "PROBE_FLOOD", "CSA_SPOOF", "SSID_CONFUSION",
})

# The three record statuses a redacted projection can carry, mirrored from the import/export layer.
STATUSES = frozenset({"admitted", "unsupported", "malformed"})

# The FIXED safe field allowlist. Every record is projected to exactly these fields before it is searched or
# returned; a missing field takes its neutral default and any extra key is dropped. This re-enforces the export
# redaction boundary here, so search can never surface a field the export projection would not.
_RECORD_FIELDS: Tuple[str, ...] = ("line", "status", "category", "ts", "epoch", "reason")

# Only textual metadata fields are inspected by the plain-text query. line/ts/epoch are numeric identity/clock,
# not free text, and are excluded from substring matching so "plain text" stays textual and unambiguous.
SEARCHABLE_FIELDS: Tuple[str, ...] = ("status", "category", "reason")

MAX_RECORDS = 10_000          # mirrors incident_import.MAX_LINES; a larger supplied set is a fixed error
MAX_QUERY_CHARS = 256         # plain-text query bound; a longer query is a fixed error, never truncated-and-run

# Sentinels meaning "do not filter on this dimension" — the empty string and the UI's "all" option, plus None.
_ANY = frozenset({"all", ""})


class SearchError(ValueError):
    """A bounded, fixed-code search-input error. The ``code`` is a constant label; no supplied value is ever
    interpolated into it, so an error can never echo raw record content back to a caller or a log."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _safe_record(rec: Mapping[str, Any]) -> Dict[str, Any]:
    """Project one supplied record to the fixed safe field allowlist, dropping every unlisted key.

    ``line``/``ts``/``epoch`` keep their value only when it is a real ``int`` (``bool`` excluded), else ``None``;
    ``status``/``category``/``reason`` keep their value only when it is a real ``str``, else ``""``. This means a
    caller cannot smuggle a non-string object into the text scan, and cannot surface any field outside the
    allowlist."""
    if not isinstance(rec, Mapping):
        raise SearchError("record-not-a-mapping")
    out: Dict[str, Any] = {}
    for key in ("line", "ts", "epoch"):
        val = rec.get(key)
        out[key] = val if (type(val) is int) else None
    for key in ("status", "category", "reason"):
        val = rec.get(key)
        out[key] = val if isinstance(val, str) else ""
    return out


def _sort_key(rec: Mapping[str, Any]) -> Tuple[Any, ...]:
    """A total-order deterministic sort key: real positions first in ascending order, position-less records
    last, then status/category/reason to break any tie. Never compares an int against ``None`` (which would
    raise), because the leading flag separates the two groups."""
    line = rec.get("line")
    has_line = type(line) is int
    return (0 if has_line else 1, line if has_line else 0,
            rec.get("status") or "", rec.get("category") or "", rec.get("reason") or "")


def _normalize_choice(value: Optional[str], allowed: "frozenset[str]", error_code: str) -> Optional[str]:
    """Validate an optional exact-match filter (category/status). ``None`` or an "any" sentinel means no filter
    on this dimension; a value in *allowed* is that filter; anything else is a fixed error. A non-string filter
    (other than ``None``) is a type error — the caller passed the wrong shape."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{error_code} filter must be a string or None")
    if value in _ANY:
        return None
    if value not in allowed:
        raise SearchError(error_code)
    return value


def _normalize_text(text: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Validate the optional plain-text query. Returns ``(needle_lower, echo)`` where *needle_lower* is the
    lowercased substring actually matched (or ``None`` for no text filter) and *echo* is the trimmed original
    for reporting. A non-string (other than ``None``) is a type error; a query longer than the bound is a fixed
    error BEFORE any scan; an all-whitespace/empty query is simply no text filter."""
    if text is None:
        return None, None
    if not isinstance(text, str):
        raise TypeError("text query must be a string or None")
    if len(text) > MAX_QUERY_CHARS:
        raise SearchError("query-too-long")
    trimmed = text.strip()
    if not trimmed:
        return None, None
    return trimmed.lower(), trimmed


def search_records(records: Sequence[Mapping[str, Any]], *, text: Optional[str] = None,
                   category: Optional[str] = None, status: Optional[str] = None) -> Dict[str, Any]:
    """Narrow *records* by an optional plain-text substring, category, and status (combined with AND).

    *records* is the caller-supplied list of already-redacted workspace rows (see
    :func:`records_from_projection`). Each is projected to the fixed safe allowlist before matching, so the
    result never carries a field the export projection would not. Matching is deterministic and pure; the
    returned ``matched`` list is in a fixed order. Bounds (record count, query length) and the category/status
    allowlists are validated before any scan. Never raises on ordinary content — only on a shape/bound
    violation (``TypeError`` / :class:`SearchError`)."""
    if isinstance(records, (str, bytes, bytearray)) or not isinstance(records, Sequence):
        raise TypeError("records must be a sequence of record mappings")
    if len(records) > MAX_RECORDS:
        raise SearchError("too-many-records")

    needle, echo_text = _normalize_text(text)
    cat = _normalize_choice(category, SUPPORTED_TYPES, "unknown-category")
    st = _normalize_choice(status, STATUSES, "unknown-status")

    matched: List[Dict[str, Any]] = []
    for raw in records:
        rec = _safe_record(raw)
        if st is not None and rec["status"] != st:
            continue
        if cat is not None and rec["category"] != cat:
            continue
        if needle is not None and not any(needle in rec[f].lower() for f in SEARCHABLE_FIELDS):
            continue
        matched.append(rec)

    matched.sort(key=_sort_key)
    return {
        "matched": matched,
        "match_count": len(matched),
        "total_records": len(records),
        "query": {"text": echo_text, "category": cat, "status": st},
    }


def _projection_group(projection: Mapping[str, Any], name: str) -> Sequence[Any]:
    """Return one projection group as a validated sequence. A missing group (absent key or ``None``) is an
    empty group. A PRESENT group of any other shape (a string, a scalar, a mapping) is a fixed-code error, so a
    supplied ``{"events": "bad"}`` can never be iterated character-by-character into invented rows. The item
    SHAPE is checked later, while building rows."""
    val = projection.get(name)
    if val is None:
        return ()
    if not isinstance(val, (list, tuple)):
        raise SearchError("malformed-projection-group")
    return val


def records_from_projection(projection: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Build the unified searchable row list from a redacted export projection (the Python-side counterpart of
    the browser's row builder), so the search seam can consume exactly what ``export_projection`` produces.

    Admitted events become ``admitted`` rows (with their fixed label + claimed clock); unsupported/malformed
    entries become ``unsupported``/``malformed`` rows carrying only position + fixed reason. Duplicates are NOT
    collapsed: each source entry keeps its own position as a distinct identity.

    Shape is validated up front and no row is invented for malformed input: each of ``events``/``unsupported``/
    ``malformed`` must be absent or a list/tuple (else :class:`SearchError` ``malformed-projection-group``), and
    every entry inside a group must be a mapping (else ``malformed-projection-item``). The total-size bound is
    enforced from the declared group lengths BEFORE any row is traversed or allocated, so an over-cap projection
    is rejected with ``too-many-records`` without first walking (or partially building) its entries. Every fixed
    code is a constant label; no supplied value is interpolated into it."""
    if not isinstance(projection, Mapping):
        raise SearchError("projection-not-a-mapping")
    events = _projection_group(projection, "events")
    unsupported = _projection_group(projection, "unsupported")
    malformed = _projection_group(projection, "malformed")
    # Bound the total from the declared group lengths BEFORE traversing/allocating any row.
    if len(events) + len(unsupported) + len(malformed) > MAX_RECORDS:
        raise SearchError("too-many-records")
    rows: List[Dict[str, Any]] = []
    for e in events:
        if not isinstance(e, Mapping):
            raise SearchError("malformed-projection-item")
        rows.append(_safe_record({"line": e.get("line"), "status": "admitted",
                                   "category": e.get("category"), "ts": e.get("ts"),
                                   "epoch": e.get("epoch"), "reason": ""}))
    for group, items in (("unsupported", unsupported), ("malformed", malformed)):
        for it in items:
            if not isinstance(it, Mapping):
                raise SearchError("malformed-projection-item")
            rows.append(_safe_record({"line": it.get("line"), "status": group,
                                      "category": "", "ts": None, "epoch": None,
                                      "reason": it.get("reason")}))
    return rows
