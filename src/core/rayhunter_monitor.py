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

import hashlib
import html
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

#: The normalized report version this adapter understands (tagged v0.12.0 `REPORT_VERSION`).
SUPPORTED_REPORT_VERSION = 2


def parse_analysis_report(
    data: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_REPORT_BYTES,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    max_events: int = DEFAULT_MAX_EVENTS,
    pre_truncated: bool = False,
) -> Dict[str, Any]:
    """Parse a Rayhunter analysis-report NDJSON blob into a bounded, honest summary. Pure — no I/O.

    Schema is the tagged v0.12.0 normalized (report_version 2): a ``ReportMetadata`` first line
    (``analyzers`` list + ``report_version``), then ``AnalysisRow`` lines with ``packet_timestamp``,
    ``skipped_message_reason`` (singular), and a POSITIONAL ``events`` list where ``events[i]`` belongs to
    ``metadata.analyzers[i]``. Event ``event_type`` is a string ("High"…) or a legacy object.

    ``complete`` is derived from EVERY loss condition — a byte/line/event truncation, a malformed or
    oversized line, an unsupported report version, or an unknown severity all make it False with the
    reasons joined in ``coverage``. So a caller can never read a partial/garbled report as a complete
    zero-warning result. Never raises: a bad line is counted (malformed/oversized), never crashed on and
    never guessed into an event. ``pre_truncated`` lets a bounded fetch signal the bytes were already cut."""
    counts_by_level: Dict[str, int] = {lvl: 0 for lvl in LEVELS}
    events: List[Dict[str, Any]] = []
    metadata: Optional[Dict[str, Any]] = None
    analyzer_names: List[Optional[str]] = []
    malformed = oversized = skipped = total_rows = 0
    losses: set = set()
    if pre_truncated:
        losses.add("truncated-before-parse")

    if len(data) > max_bytes:
        data = data[:max_bytes]
        losses.add("truncated-bytes")

    for raw_line in data.split(b"\n"):
        line = raw_line.strip()
        if not line:
            continue
        if len(line) > max_line_bytes:
            oversized += 1
            losses.add("oversized-line")
            continue
        try:
            obj = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            malformed += 1
            losses.add("malformed-line")
            continue
        if not isinstance(obj, dict):
            malformed += 1
            losses.add("malformed-line")
            continue
        # First metadata object. Supported metadata (v2) REQUIRES report_version == SUPPORTED and an
        # analyzers LIST — anything short of that can't back a "complete" verdict (positional event→analyzer
        # mapping and the version contract both depend on it), so each shortfall is its own loss.
        if metadata is None and _looks_like_metadata(obj):
            metadata = obj
            rv = obj.get("report_version")
            if rv is None:
                losses.add("missing-report-version")
            elif rv != SUPPORTED_REPORT_VERSION:
                losses.add("unsupported-report-version")
            an = obj.get("analyzers")
            if isinstance(an, list):
                analyzer_names = [_analyzer_name(a) for a in an]
            else:
                losses.add("malformed-metadata")   # analyzers absent / not a list -> unusable metadata
            continue
        total_rows += 1
        reason = obj.get("skipped_message_reason")
        if isinstance(reason, str) and reason:
            skipped += 1
            continue
        ts = obj.get("packet_timestamp")   # optional; a missing timestamp stays None, never epoch
        raw_events = obj.get("events")
        if not isinstance(raw_events, list):
            # A non-skipped AnalysisRow must carry an events LIST (empty or with null slots is fine). A
            # missing or non-list `events` (e.g. events:42, or the key absent) is a malformed row whose
            # warning contribution can't be trusted — never skip it silently as zero warnings.
            losses.add("malformed-row")
            continue
        truncated = False
        for i, ev in enumerate(raw_events):
            if ev is None:
                continue  # a LEGITIMATE null positional slot (that analyzer produced no event)
            if not isinstance(ev, dict):
                # a scalar/list where an Event object belongs (e.g. events:[42]) is CORRUPT, not a
                # null slot — mark coverage incomplete, never silently drop it.
                losses.add("malformed-event")
                continue
            if len(events) >= max_events:
                losses.add("truncated-events")
                truncated = True
                break
            level = _event_level(ev.get("event_type"))
            if level in counts_by_level:
                counts_by_level[level] += 1
            else:
                # unknown string ("Critical") OR an unparseable event_type ({}/missing) -> the warning
                # tally can't be trusted, so coverage is incomplete.
                losses.add("unknown-severity")
            analyzer = analyzer_names[i] if i < len(analyzer_names) else None
            events.append({
                "level": level,
                "timestamp": ts,
                "analyzer": analyzer,
                "message": ev.get("message") if isinstance(ev.get("message"), str) else None,
            })
        if truncated:
            break

    if metadata is None:
        # No ReportMetadata line at all — we can't establish report version / analyzer identity, so the
        # coverage is not complete (missing supported metadata can't back a "complete" verdict).
        losses.add("missing-metadata")

    warnings = sum(counts_by_level[lvl] for lvl in _WARNING_LEVELS)
    complete = not losses
    return {
        "metadata": metadata,
        "report_version": metadata.get("report_version") if isinstance(metadata, dict) else None,
        "analyzers": analyzer_names,
        "counts": {
            "by_level": counts_by_level,
            "warnings": warnings,
            "informational": counts_by_level["Informational"],
            "skipped": skipped,
            "events": len(events),
        },
        "events": events,
        "complete": complete,
        "coverage": "complete" if complete else ",".join(sorted(losses)),
        "malformed_lines": malformed,
        "oversized_lines": oversized,
        "total_rows": total_rows,
    }


def _looks_like_metadata(obj: Dict[str, Any]) -> bool:
    """A ReportMetadata line carries ``analyzers`` / ``report_version`` and NONE of the row fields."""
    keys = set(obj.keys())
    return bool(keys & {"analyzers", "report_version"}) and not (
        keys & {"events", "packet_timestamp", "skipped_message_reason"})


def _event_level(event_type: Any) -> Optional[str]:
    """Level from an event's ``event_type`` — a plain string in normalized v2, or a legacy object
    (``{"type":"Informational"}`` / ``{"type":"QualitativeWarning","severity":"High"}``). Unknown → None."""
    if isinstance(event_type, str):
        return event_type
    if isinstance(event_type, dict):
        if event_type.get("type") == "Informational":
            return "Informational"
        sev = event_type.get("severity")
        return sev if isinstance(sev, str) else None
    return None


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
    be None (endpoint unreachable/failed) — that surfaces as an unknown indicator, not a healthy default.
    A non-dict device reply is treated as absent (untrusted input never reaches a ``.get``)."""
    system_stats = system_stats if isinstance(system_stats, dict) else None
    manifest = manifest if isinstance(manifest, dict) else None
    analysis = analysis if isinstance(analysis, dict) else None
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
        obj = json.loads(body.decode("utf-8"))
        # These endpoints return JSON OBJECTS — a non-dict root is an untrusted/invalid device reply.
        return obj if isinstance(obj, dict) else None
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


# --------------------------------------------------------------------------- #
# Phase 2 — fetch + parse ONE analysis report by (validated) name, and build a redacted, escaped,
# self-contained local export. Still read-only; the report NAME is validated as a single path segment
# AND checked against the manifest, so no path traversal / arbitrary file reaches the device request.
# --------------------------------------------------------------------------- #

#: A report name must be a single, safe path segment: alnum / dash / underscore / dot, no ".." run.
_REPORT_NAME_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")

#: Events are heuristic observations. This wording is embedded verbatim in every export.
EXPORT_DISCLAIMER = (
    "These are heuristic observations from Rayhunter's analyzers. They are NOT proof of an IMSI "
    "catcher / cell-site simulator, and do NOT identify an operator. False positives are expected. "
    "A reachable daemon is not proof of useful capture. A live snapshot is not a finalized capture."
)


def _valid_report_name(name: str) -> bool:
    if not name or len(name) > 128 or ".." in name:
        return False
    return all(c in _REPORT_NAME_OK for c in name)


def _name_in_manifest(name: str, manifest: Dict[str, Any]) -> bool:
    """True only if *name* is an actual entry (or the current entry) in the manifest — so a report fetch
    can never be pointed at an arbitrary name even if it passes the character check."""
    entries = manifest.get("entries") if isinstance(manifest, dict) else None
    known = set()
    if isinstance(entries, list):
        for e in entries:
            if isinstance(e, dict) and isinstance(e.get("name"), str):
                known.add(e["name"])
    cur = manifest.get("current_entry") if isinstance(manifest, dict) else None
    if isinstance(cur, dict) and isinstance(cur.get("name"), str):
        known.add(cur["name"])
    return name in known


def _fetch_bytes(admin_ip: str, path: str, *, timeout: float, max_bytes: int,
                 getter: Optional[Callable]) -> Optional[bytes]:
    from src.core.backends import adb_backend
    if not adb_backend._valid_ipv4(admin_ip) or not adb_backend._is_local_ipv4(admin_ip):
        return None
    get = getter or adb_backend.requests.get
    url = "http://" + admin_ip + ":8080" + path
    r = None
    try:
        r = get(url, timeout=timeout, allow_redirects=False, stream=True)
        if getattr(r, "status_code", None) != 200:
            return None
        # Read ONE byte past the budget and return it UNSLICED — if the report overflowed, the parser
        # sees len > max_bytes and marks it incomplete. Slicing to exactly max_bytes here would hide the
        # overflow and let an oversized, warning-bearing report read as complete + zero-warnings.
        return r.raw.read(max_bytes + 1, decode_content=True)
    except Exception:  # noqa: BLE001
        return None
    finally:
        _safe_close(r)


def fetch_report(admin_ip: str, name: str, *, manifest: Optional[Dict[str, Any]] = None,
                 timeout: float = 8.0, max_bytes: int = DEFAULT_MAX_REPORT_BYTES,
                 getter: Optional[Callable] = None) -> Optional[Dict[str, Any]]:
    """Fetch + parse ONE analysis report by name. The name must be a safe single path segment AND (if a
    manifest is given) an actual entry in it. Returns the parsed summary, or None. Never raises."""
    if not _valid_report_name(name):
        return None
    if manifest is not None and not _name_in_manifest(name, manifest):
        return None
    raw = _fetch_bytes(admin_ip, "/api/analysis-report/" + name,
                       timeout=timeout, max_bytes=max_bytes, getter=getter)
    if raw is None:
        return None
    return parse_analysis_report(raw, max_bytes=max_bytes)


def build_report_export(snapshot: Dict[str, Any], parsed: Dict[str, Any], *, cc_version: str,
                        exported_at: str, selected_recording: Optional[str] = None,
                        capture_interval: Optional[str] = None,
                        include_detailed: bool = False) -> Dict[str, Any]:
    """Build a self-contained, ESCAPED HTML + machine-readable JSON export from a consistent snapshot.

    Redaction (default): NO precise GPS, NO IP/device identifiers, and NO raw free-text event messages
    (which may carry identifiers) — only structural fields + summaries. ``include_detailed=True`` includes
    the raw messages (a local, explicit opt-in). Every event is labeled heuristic (EXPORT_DISCLAIMER), a
    partial report is never presented as complete, unknown values stay unknown, and a SHA-256 digest of
    the JSON is included for reproducibility (a digest does not authenticate radio origin). Pure — the
    caller stamps ``exported_at``/``cc_version`` so this stays deterministic + testable."""
    counts = parsed.get("counts", {})
    events_out = []
    for ev in parsed.get("events", []):
        row = {
            "level": ev.get("level"),
            "timestamp": ev.get("timestamp"),      # unknown stays None, never epoch
            "analyzer": ev.get("analyzer"),
        }
        if include_detailed:
            row["message"] = ev.get("message")
        else:
            row["message_redacted"] = ev.get("message") is not None
        events_out.append(row)

    meta = parsed.get("metadata") or {}
    payload = {
        "kind": "cyber-controller-rayhunter-export",
        "cc_version": cc_version,
        "rayhunter_version": snapshot.get("version"),
        "exported_at": exported_at,
        "selected_recording": selected_recording,
        "capture_interval": capture_interval,
        "transport": snapshot.get("transport"),
        "recording": snapshot.get("recording"),
        "report_version": meta.get("report_version") if isinstance(meta, dict) else None,
        "analyzers": meta.get("analyzers") if isinstance(meta, dict) else None,
        "counts": counts,
        "coverage": parsed.get("coverage"),
        "complete": parsed.get("complete"),
        "malformed_lines": parsed.get("malformed_lines"),
        "oversized_lines": parsed.get("oversized_lines"),
        "detailed_evidence_included": bool(include_detailed),
        "disclaimer": EXPORT_DISCLAIMER,
        "events": events_out,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    payload["digest_sha256"] = digest
    return {"json": payload, "digest": digest, "html": _render_export_html(payload)}


def _render_export_html(p: Dict[str, Any]) -> str:
    """Render the export payload to a self-contained HTML doc, ESCAPING every dynamic field (device text
    is untrusted). No external assets, no scripts."""
    def e(v: Any) -> str:
        return html.escape("" if v is None else str(v))

    c = p.get("counts", {}) or {}
    by = c.get("by_level", {}) or {}
    rows = []
    for ev in p.get("events", []):
        msg = ev.get("message") if "message" in ev else ("(redacted)" if ev.get("message_redacted") else "")
        rows.append(
            "<tr><td>" + e(ev.get("level")) + "</td><td>" + e(ev.get("timestamp"))
            + "</td><td>" + e(ev.get("analyzer")) + "</td><td>" + e(msg) + "</td></tr>")
    complete_note = "" if p.get("complete") else (
        "<p class='warn'>Incomplete coverage (" + e(p.get("coverage")) + ") — partial totals, not a full report.</p>")
    return (
        "<!doctype html><meta charset='utf-8'><title>Rayhunter report — " + e(p.get("exported_at"))
        + "</title><style>body{font:14px system-ui;margin:2rem;max-width:60rem}"
        "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:4px 8px;text-align:left}"
        ".warn{color:#a00}.muted{color:#666}</style>"
        "<h1>Rayhunter analysis report</h1>"
        "<p class='muted'>CC " + e(p.get("cc_version")) + " · Rayhunter " + e(p.get("rayhunter_version"))
        + " · exported " + e(p.get("exported_at")) + " · recording " + e(p.get("selected_recording")) + "</p>"
        "<p><b>Warnings:</b> " + e(c.get("warnings")) + " (High " + e(by.get("High")) + ", Medium "
        + e(by.get("Medium")) + ", Low " + e(by.get("Low")) + ") · <b>Informational:</b> "
        + e(c.get("informational")) + " · <b>Skipped:</b> " + e(c.get("skipped")) + "</p>"
        + complete_note
        + "<p class='muted'>" + e(p.get("disclaimer")) + "</p>"
        + "<table><thead><tr><th>Level</th><th>Time</th><th>Analyzer</th><th>Message</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table>"
        + "<p class='muted'>digest sha256: " + e(p.get("digest_sha256")) + "</p>")
