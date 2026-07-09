"""Scan-to-export: CSV rendering of the shared target pool (1.7.0).

Covers column order, CSV formula-injection neutralisation on untrusted SSIDs, numeric fields staying raw
(a negative RSSI must not get quote-prefixed), the empty pool, and the file-writing round-trip.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.core.target_export import (
    TARGET_CSV_COLUMNS,
    export_targets_csv,
    target_to_csv_row,
    targets_to_csv,
)
from src.models.target import Target, TargetType

_TS = datetime(2026, 7, 9, 3, 30, 0, tzinfo=timezone.utc)


def _ap(mac: str, ssid: str, rssi: int = -50, channel: int = 6, **kw) -> Target:
    return Target(
        target_type=TargetType.AP, mac=mac, ssid=ssid, rssi=rssi, channel=channel,
        timestamp=_TS, last_seen=_TS, **kw,
    )


def test_header_and_row_columns():
    row = target_to_csv_row(_ap("AA:BB:CC:DD:EE:FF", "HomeNet", rssi=-42, channel=11,
                                 device_source="COM16", encryption="WPA2", vendor="Espressif"))
    assert row.split(",") == [
        TargetType.AP.value, "HomeNet", "AA:BB:CC:DD:EE:FF", "-42", "11", "COM16", "WPA2", "Espressif",
        _TS.isoformat(), _TS.isoformat(),
    ]
    csv = targets_to_csv([])
    assert csv.splitlines()[0] == ",".join(TARGET_CSV_COLUMNS)


def test_csv_formula_injection_is_neutralised():
    # An attacker-chosen SSID that a spreadsheet would evaluate as a formula must be de-fanged.
    row = target_to_csv_row(_ap("AA:AA:AA:AA:AA:AA", "=cmd|'/c calc'!A1"))
    ssid_cell = row.split(",", 2)[1]
    assert ssid_cell.startswith("'=") or ssid_cell.startswith('"\'=')


def test_negative_rssi_stays_raw_numeric():
    # RSSI is numeric, not routed through the formula guard — "-42" must NOT be quote-prefixed.
    row = target_to_csv_row(_ap("AA:AA:AA:AA:AA:01", "Net", rssi=-42))
    assert ",-42," in "," + row + ","


def test_ssid_with_comma_is_quoted():
    row = target_to_csv_row(_ap("AA:AA:AA:AA:AA:02", "My, Net"))
    assert '"My, Net"' in row


def test_empty_pool_is_header_only():
    csv = targets_to_csv([])
    assert csv == ",".join(TARGET_CSV_COLUMNS) + "\n"


def test_export_writes_file_and_counts(tmp_path):
    targets = [
        _ap("AA:AA:AA:AA:AA:01", "Alpha"),
        _ap("AA:AA:AA:AA:AA:02", "Bravo"),
    ]
    out = tmp_path / "targets.csv"
    n = export_targets_csv(targets, out)
    assert n == 2
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(TARGET_CSV_COLUMNS)
    assert len(lines) == 3  # header + 2 rows
    assert lines[1].split(",")[1] == "Alpha"
    assert lines[2].split(",")[1] == "Bravo"
