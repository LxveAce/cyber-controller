"""Capture export (punch-list #2, slice 4): CaptureStore -> CSV / JSON, mirroring target_export.

Covers the column contract, the OWASP CSV-injection guard on attacker-influenced text, raw numerics,
honest-null GPS handling, and a JSON round-trip through from_dict. No Qt / no hardware.
"""
from __future__ import annotations

import json

from src.core.capture_export import (
    CAPTURE_CSV_COLUMNS,
    capture_to_csv_row,
    captures_to_csv,
    export_captures_csv,
    export_captures_json,
)
from src.models.capture import CaptureRecord


def _sample() -> CaptureRecord:
    return CaptureRecord(
        bssid="AA:BB:CC:DD:EE:FF", capture_type="eapol", ssid="HomeNet", channel=6,
        sta_mac="11:22:33:44:55:66", key_version=2, rssi=-52, gps_lat=37.5, gps_lon=-122.3,
        device_source="COM7", firmware="marauder", pmkid="", pcap_path="/sd/hs_01.pcapng",
        hc22000_path="", hashes_extracted=1, crack_status="uncracked", password="", wordlist="",
    )


def test_header_and_row_have_matching_column_counts():
    csv = captures_to_csv([_sample()])
    lines = csv.splitlines()
    header = lines[0].split(",")
    assert tuple(header) == CAPTURE_CSV_COLUMNS
    assert len(lines[1].split(",")) == len(CAPTURE_CSV_COLUMNS)


def test_numeric_columns_emitted_raw_not_quoted():
    # A legit negative RSSI must NOT be quote-prefixed (the target_export caveat this clones).
    row = capture_to_csv_row(_sample())
    cells = row.split(",")
    assert cells[CAPTURE_CSV_COLUMNS.index("rssi")] == "-52"
    assert cells[CAPTURE_CSV_COLUMNS.index("channel")] == "6"
    assert cells[CAPTURE_CSV_COLUMNS.index("key_version")] == "2"
    assert cells[CAPTURE_CSV_COLUMNS.index("times_seen")] == "1"


def test_csv_injection_guard_on_ssid():
    # A formula-smuggling SSID must be neutralised by _csv_field (OWASP CSV injection).
    rec = _sample()
    rec.ssid = "=cmd|'/c calc'!A1"
    row = capture_to_csv_row(rec)
    ssid_cell = row.split(",")[CAPTURE_CSV_COLUMNS.index("ssid")]
    assert not ssid_cell.startswith("=")          # neutralised, not a live formula


def test_gps_none_is_empty_float_is_repr():
    rec = _sample()
    rec.gps_lat = None
    rec.gps_lon = -122.3
    cells = capture_to_csv_row(rec).split(",")
    assert cells[CAPTURE_CSV_COLUMNS.index("gps_lat")] == ""       # honest null, not "0"
    assert cells[CAPTURE_CSV_COLUMNS.index("gps_lon")] == repr(-122.3)


def test_export_csv_writes_file_and_returns_count(tmp_path):
    path = tmp_path / "caps.csv"
    n = export_captures_csv([_sample(), _sample()], str(path))
    assert n == 2
    body = path.read_text(encoding="utf-8")
    assert body.startswith(",".join(CAPTURE_CSV_COLUMNS))
    assert body.endswith("\n")
    assert len(body.splitlines()) == 3            # header + 2 rows


def test_export_json_round_trips_through_from_dict(tmp_path):
    path = tmp_path / "caps.json"
    n = export_captures_json([_sample()], str(path))
    assert n == 1
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) == 1
    back = CaptureRecord.from_dict(data[0])
    assert back.bssid == "AA:BB:CC:DD:EE:FF" and back.ssid == "HomeNet" and back.rssi == -52
    assert back.password == ""                    # cracked-PSK column present but empty here
