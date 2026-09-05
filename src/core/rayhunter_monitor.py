"""Read-only Rayhunter monitoring — Phase 1 of the live-view feature.

A version-aware, READ-ONLY client for an already-running Rayhunter daemon (EFF, on the Orbic RC400L).
It fetches the device's status + recording manifest + analysis and builds a bounded, honest snapshot
with three distinct indicators (transport freshness, recording state, analysis warnings). It does NOT
install, configure, record, delete, or inject GPS — those stay separate (see ``adb_backend``).

Grounded in the tagged upstream contract (EFForg/rayhunter **v0.12.0**), per the reviewed plan:

* ``GET /api/system-stats`` → ``disk_stats`` / ``memory_stats`` / ``runtime_metadata``
  (rayhunter_version, system_os, arch) + optional ``battery_status``. **No CPU field** — we don't invent one.
* ``GET /api/qmdl-manifest`` → ``entries`` + nullable ``current_entry`` (name, start/last-message time,
  QMDL size, stop reason, GPS mode). The current entry identifies an active recording.
* ``GET /api/analysis`` → ``running`` (ONE nullable recording name, not an array), ``queued`` + ``finished``
  (name arrays). A running reanalysis job is distinct from an active recording.
* ``GET /api/analysis-report/{name}`` → **NDJSON** (one JSON object per line): a metadata object first,
  then packet-analysis / skipped rows. Event levels are Informational / Low / Medium / High. Counts are
  derived from the rows. Malformed lines can occur and are flagged, never silently dropped.

Honesty rules baked in (from the plan): "no warnings observed in the available report" is fine; "safe" /
"no IMSI catcher" / identifying an operator is NOT. A running daemon or successful install alone does not
establish useful capture — a missing SIM, stalled traffic, or stopped recording must not look healthy
just because a request succeeded. Unknown values stay unknown (never a fabricated 0/epoch). Device replies
are untrusted: every read is bounded (total bytes, per-line bytes, event count) and schema-checked.

Phase 1 is on-demand fetch + bounded parse (pure, fixture-tested here). The bounded snapshot cache + a
per-device polling worker, the event timeline + local HTML/JSON report export with redaction (Phase 2),
and the real-Orbic transport/capture validation (Phase 3) are the following increments. Flash/serial and
hardware freshness are not established by this module.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

# Severity levels in ascending order (upstream v0.12 analysis rows). "Informational" is NOT a warning.
LEVELS = ("Informational", "Low", "Medium", "High")
_WARNING_LEVELS = ("Low", "Medium", "High")

# Default read budgets (the plan's initial proposal; benchmark + tune before Phase 3). Bounding EVERY read
# is a hard requirement — a hostile/oversized device reply must not exhaust memory or hang the UI.
DEFAULT_MAX_REPORT_BYTES = 8 * 1024 * 1024   # 8 MiB per report
DEFAULT_MAX_LINE_BYTES = 256 * 1024          # 256 KiB per NDJSON line
DEFAULT_MAX_EVENTS = 20_000                  # retained events cap


class ReportCoverage:
    """Why a parsed report may be INCOMPLETE, so the UI never shows a partial total as complete."""

    COMPLETE = "complete"
    TRUNCATED_BYTES = "truncated:report-byte-budget"
    TRUNCATED_EVENTS = "truncated:event-budget"


def parse_analysis_report(
    data: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_REPORT_BYTES,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    max_events: int = DEFAULT_MAX_EVENTS,
) -> Dict[str, Any]:
    """Parse a Rayhunter analysis-report NDJSON blob into a bounded, honest summary. Pure — no I/O.

    Returns ``{metadata, counts, events, complete, coverage, malformed_lines, oversized_lines,
    total_rows}``. ``counts`` has ``by_level`` (per LEVELS), ``warnings`` (Low+Medium+High),
    ``informational``, ``skipped``, and ``events`` (retained). Never raises: a malformed line is COUNTED
    (``malformed_lines``) and skipped from the event tally, not silently dropped and not crashed on; a
    line over ``max_line_bytes`` is counted (``oversized_lines``) and skipped; hitting the byte or event
    budget sets ``complete=False`` with the reason, so a partial total is never presented as whole.
    """
    counts_by_level: Dict[str, int] = {lvl: 0 for lvl in LEVELS}
    events: List[Dict[str, Any]] = []
    metadata: Optional[Dict[str, Any]] = None
    malformed = 0
    oversized = 0
    skipped = 0
    total_rows = 0
    coverage = ReportCoverage.COMPLETE
    complete = True

    if len(data) > max_bytes:
        data = data[:max_bytes]
        complete = False
        coverage = ReportCoverage.TRUNCATED_BYTES

    # Split on newlines ourselves so a split/partial trailing line is handled, not assumed well-formed.
    for raw_line in data.split(b"\n"):
        line = raw_line.strip()
        if not line:
            continue
        if len(line) > max_line_bytes:
            oversized += 1
            continue
        try:
            obj = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            malformed += 1  # a malformed/garbled line is flagged, never a crash and never a guessed event
            continue
        if not isinstance(obj, dict):
            malformed += 1
            continue
        # The FIRST well-formed object is the report metadata (analyzers + versions); the rest are rows.
        if metadata is None and _looks_like_metadata(obj):
            metadata = obj
            continue
        total_rows += 1
        if _is_skipped_row(obj):
            skipped += 1
            continue
        row_events = _events_of(obj)
        for ev in row_events:
            if len(events) >= max_events:
                complete = False
                coverage = ReportCoverage.TRUNCATED_EVENTS
                break
            level = ev.get("level")
            if level in counts_by_level:
                counts_by_level[level] += 1
            events.append(ev)
        if not complete and coverage == ReportCoverage.TRUNCATED_EVENTS:
            break

    warnings = sum(counts_by_level[lvl] for lvl in _WARNING_LEVELS)
    return {
        "metadata": metadata,
        "counts": {
            "by_level": counts_by_level,
            "warnings": warnings,
            "informational": counts_by_level["Informational"],
            "skipped": skipped,
            "events": len(events),
        },
        "events": events,
        "complete": complete,
        "coverage": coverage,
        "malformed_lines": malformed,
        "oversized_lines": oversized,
        "total_rows": total_rows,
    }


def _looks_like_metadata(obj: Dict[str, Any]) -> bool:
    """A report metadata object carries analyzer/version info, not a packet timestamp + events."""
    keys = set(obj.keys())
    return bool(keys & {"analyzers", "rayhunter", "report_version", "metadata", "version"}) and \
        "events" not in keys


def _is_skipped_row(obj: Dict[str, Any]) -> bool:
    """A 'skipped' row records that a packet couldn't be analyzed (a reason, no events)."""
    if "skipped_message_reasons" in obj or "skipped_reasons" in obj or obj.get("skipped"):
        return True
    return obj.get("type") == "skipped"


def _events_of(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize a packet-analysis row into a list of ``{level, timestamp, analyzer, message}`` events.

    Positional nullable events (upstream keeps analyzer position); a null entry contributes nothing.
    A MISSING timestamp stays ``None`` (never coerced to the Unix epoch — that would fabricate a time).
    An unknown/absent level is preserved verbatim so the caller can surface a compatibility notice rather
    than miscount it as a known level.
    """
    ts = obj.get("timestamp")  # optional in Rust; unknown stays unknown
    out: List[Dict[str, Any]] = []
    analyzers = obj.get("analyzers")
    raw_events = obj.get("events")
    if isinstance(raw_events, list):
        for i, ev in enumerate(raw_events):
            if not isinstance(ev, dict):
                continue  # a null/absent positional event contributes nothing
            analyzer = None
            if isinstance(analyzers, list) and i < len(analyzers):
                analyzer = analyzers[i]
            out.append({
                "level": ev.get("event_type") or ev.get("level"),
                "timestamp": ts,
                "analyzer": _analyzer_name(analyzer) or _analyzer_name(ev.get("analyzer")),
                "message": ev.get("message") if isinstance(ev.get("message"), str) else None,
            })
    return out


def _analyzer_name(a: Any) -> Optional[str]:
    if isinstance(a, str):
        return a
    if isinstance(a, dict):
        name = a.get("name")
        return name if isinstance(name, str) else None
    return None


# --------------------------------------------------------------------------- #
# Snapshot — on-demand read of a running daemon (Phase 1). Address validation, no-redirect + response
# budgets come from adb_backend (the R01 hardening). Never raises; unknowns stay unknown.
# --------------------------------------------------------------------------- #

def build_snapshot(
    system_stats: Optional[Dict[str, Any]],
    manifest: Optional[Dict[str, Any]],
    analysis: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Combine the three read endpoints into the honest three-indicator snapshot (pure). Any argument may
    be None (endpoint unreachable/failed) — that surfaces as an unknown indicator, not a healthy default."""
    rt = (system_stats or {}).get("runtime_metadata") or {}
    version = rt.get("rayhunter_version") if isinstance(rt, dict) else None

    current = (manifest or {}).get("current_entry") if isinstance(manifest, dict) else None
    recording_name = None
    recording = "unknown"
    if manifest is not None:
        recording = "recording" if current else "stopped"
        if isinstance(current, dict):
            recording_name = current.get("name")

    running_reanalysis = None
    if isinstance(analysis, dict):
        running_reanalysis = analysis.get("running")  # ONE nullable name, not an array

    return {
        "transport": "ok" if system_stats is not None else "unreachable",
        "version": version,
        "recording": recording,               # recording | stopped | unknown
        "recording_name": recording_name,
        "reanalysis_running": running_reanalysis,
        "battery": (system_stats or {}).get("battery_status") if isinstance(system_stats, dict) else None,
        "disk": (system_stats or {}).get("disk_stats") if isinstance(system_stats, dict) else None,
        "memory": (system_stats or {}).get("memory_stats") if isinstance(system_stats, dict) else None,
        # Deliberately NOT a "safe" / "no IMSI-catcher" verdict — warning counts come from a parsed report.
        "note": "Read-only. A reachable daemon does not prove useful capture; needs a SIM + live traffic.",
    }


def fetch_json(admin_ip: str, path: str, *, timeout: float = 6.0,
               max_bytes: int = DEFAULT_MAX_REPORT_BYTES,
               getter: Optional[Callable] = None) -> Optional[Any]:
    """GET one Rayhunter JSON endpoint from a VALIDATED local device address, bounded + no-redirect.

    ``path`` must be one of the known read endpoints (allowlisted) — never an arbitrary URL. Returns the
    decoded JSON, or None on any failure (unreachable, non-200, oversized, malformed) — never raises,
    never follows a redirect, never forwards CC credentials. ``getter`` is injectable for tests."""
    from src.core.backends import adb_backend
    if not adb_backend._valid_ipv4(admin_ip) or not adb_backend._is_local_ipv4(admin_ip):
        return None
    if path not in _ALLOWED_PATHS:
        return None
    get = getter or adb_backend.requests.get
    url = "http://" + admin_ip + ":8080" + path
    try:
        r = get(url, timeout=timeout, allow_redirects=False, stream=True)
        if getattr(r, "status_code", None) != 200:
            _safe_close(r)
            return None
        body = r.raw.read(max_bytes + 1, decode_content=True)
        _safe_close(r)
        if len(body) > max_bytes:
            return None
        return json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001 — any failure is a None snapshot input, never a raise
        _safe_close(locals().get("r"))
        return None


#: The only device paths this read-only client may hit — no arbitrary URL/path is ever accepted.
_ALLOWED_PATHS = frozenset({"/api/system-stats", "/api/qmdl-manifest", "/api/analysis"})


def _safe_close(r: Any) -> None:
    try:
        if r is not None:
            r.close()
    except Exception:  # noqa: BLE001
        pass
