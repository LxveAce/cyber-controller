"""Scan-to-export: render the shared target pool to CSV.

Every access point / client / BLE device the session has seen — from the
:class:`~src.core.cross_comm.TargetPool` — written as a spreadsheet-friendly CSV so a scan can be exported
for analysis or archival.

Untrusted broadcast strings (SSID, vendor, device source, MAC) are routed through the shared
:func:`src.core.wardrive._csv_field` so a malicious SSID can't smuggle a spreadsheet formula (OWASP "CSV
Injection"). Numeric columns (RSSI / channel) are emitted as-is — routing them through ``_csv_field`` would
quote-prefix a legitimate negative RSSI like ``-52``.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.core.wardrive import _csv_field
from src.models.target import Target

# Column order for the exported CSV. Stable so downstream tooling / a re-import can rely on it.
TARGET_CSV_COLUMNS: tuple[str, ...] = (
    "type", "ssid", "mac", "rssi", "channel", "device_source", "encryption", "vendor",
    "first_seen", "last_seen",
)


def _iso(dt: Any) -> str:
    """ISO-8601 string for a datetime; tolerant of a plain string / None so a malformed field can't abort."""
    try:
        return dt.isoformat()
    except AttributeError:
        return "" if dt is None else str(dt)


def target_to_csv_row(t: Target) -> str:
    """One CSV line for a target (no trailing newline). Column order matches :data:`TARGET_CSV_COLUMNS`."""
    return ",".join([
        _csv_field(t.target_type.value),
        _csv_field(t.ssid),
        _csv_field(t.mac),
        str(int(t.rssi)),
        str(int(t.channel)),
        _csv_field(t.device_source),
        _csv_field(t.encryption),
        _csv_field(t.vendor),
        _iso(t.timestamp),
        _iso(t.last_seen),
    ])


def targets_to_csv(targets: Iterable[Target]) -> str:
    """Render targets as a full CSV document: header row + one row per target + a trailing newline."""
    lines = [",".join(TARGET_CSV_COLUMNS)]
    lines.extend(target_to_csv_row(t) for t in targets)
    return "\n".join(lines) + "\n"


def export_targets_csv(targets: Iterable[Target], path: Any) -> int:
    """Write *targets* to *path* as CSV. Returns the number of target rows written (header excluded)."""
    rows = list(targets)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(targets_to_csv(rows))
    return len(rows)
