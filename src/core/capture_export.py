"""Capture-to-export: render the shared capture store to CSV / JSON.

Every WPA handshake / PMKID the session captured — from the Crack Lab's
:class:`~src.core.capture_store.CaptureStore` — written as a spreadsheet-friendly CSV (or JSON) so a
capture log can be exported for analysis, archival, or off-box cracking. A structural clone of
:mod:`src.core.target_export` (same idioms, same guards).

Untrusted broadcast strings (SSID, BSSID, device source, firmware, PMKID hex, file paths, wordlist,
recovered password) go through the shared :func:`src.core.wardrive._csv_field` so a malicious
SSID can't smuggle a spreadsheet formula (OWASP "CSV Injection"). Numeric columns (channel / RSSI /
key version / counts) are emitted as-is — routing them through ``_csv_field`` would quote-prefix a
legit negative RSSI like ``-52``. GPS floats are ``repr()``'d (or empty for ``None``), never
quoted — a float can't inject and quoting would break a re-import.

NB: the CSV/JSON includes the recovered ``password`` once a capture is cracked. The crack flow is
consent-gated, but an exported file is plaintext — the export dialog copy warns the operator.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from src.core.wardrive import _atomic_write_text, _csv_field
from src.models.capture import CaptureRecord

# Column order for the exported CSV. Stable so downstream tooling / a re-import can rely on it.
CAPTURE_CSV_COLUMNS: tuple[str, ...] = (
    "capture_type", "bssid", "ssid", "channel", "sta_mac", "key_version",
    "rssi", "gps_lat", "gps_lon", "device_source", "firmware",
    "pmkid", "pcap_path", "hc22000_path", "hashes_extracted",
    "crack_status", "password", "wordlist",
    "captured_at", "last_seen", "times_seen",
)


def _iso(dt: Any) -> str:
    """ISO-8601 for a datetime; a plain string / None can't abort on a malformed field."""
    try:
        return dt.isoformat()
    except AttributeError:
        return "" if dt is None else str(dt)


def _gps(value: Any) -> str:
    """A GPS float as its repr, or empty for ``None`` — never ``_csv_field`` (see module doc)."""
    return "" if value is None else repr(value)


def capture_to_csv_row(c: CaptureRecord) -> str:
    """One CSV line (no trailing newline). Order matches :data:`CAPTURE_CSV_COLUMNS`."""
    return ",".join([
        _csv_field(c.capture_type),
        _csv_field(c.bssid),
        _csv_field(c.ssid),
        str(int(c.channel)),
        _csv_field(c.sta_mac),
        str(int(c.key_version)),
        str(int(c.rssi)),
        _gps(c.gps_lat),
        _gps(c.gps_lon),
        _csv_field(c.device_source),
        _csv_field(c.firmware),
        _csv_field(c.pmkid),
        _csv_field(c.pcap_path),
        _csv_field(c.hc22000_path),
        str(int(c.hashes_extracted)),
        _csv_field(c.crack_status),
        _csv_field(c.password),
        _csv_field(c.wordlist),
        _iso(c.captured_at),
        _iso(c.last_seen),
        str(int(c.times_seen)),
    ])


def captures_to_csv(caps: Iterable[CaptureRecord]) -> str:
    """Render captures as a full CSV: header row + one row per capture + a trailing newline."""
    lines = [",".join(CAPTURE_CSV_COLUMNS)]
    lines.extend(capture_to_csv_row(c) for c in caps)
    return "\n".join(lines) + "\n"


def export_captures_csv(caps: Iterable[CaptureRecord], path: Any) -> int:
    """Write *caps* to *path* as CSV. Returns the number of rows written (header excluded)."""
    rows = list(caps)
    _atomic_write_text(path, captures_to_csv(rows))  # atomic temp->replace
    return len(rows)


def export_captures_json(caps: Iterable[CaptureRecord], path: Any) -> int:
    """Write *caps* to *path* as a JSON array of ``to_dict()`` rows. Returns the row count."""
    rows = [c.to_dict() for c in caps]
    _atomic_write_text(path, json.dumps(rows, indent=2))  # atomic temp->replace
    return len(rows)
