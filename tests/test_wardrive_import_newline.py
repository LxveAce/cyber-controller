"""Record-ending (CR / LF / CRLF) compatibility for the CSV import readers.

Both `iter_wigle_rows` and `div_native_to_points` build `csv.reader(io.StringIO(text, newline=""))`.
`newline=""` hands the raw text to the csv module so it splits records on CR, LF, or CRLF (a slippy
detail: a plain `io.StringIO(text)` raised "new-line character seen in unquoted field" on CR-only
WiGLE and silently returned no points for CR-only DIV). Focused on the two readers; no app, capture,
GPS, device, or file I/O.
"""
from __future__ import annotations

import pytest

from src.core import wardrive_import as w

_WIGLE_ROWS = [
    "WigleWifi-1.6,appRelease=1,model=x",
    "MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,CurrentLatitude,CurrentLongitude,"
    "AltitudeMeters,AccuracyMeters,RCOIs,MfgrId,Type",
    "00:11:22:33:44:55,NetOne,[WPA2],2020-01-01,6,2437,-50,40.1,-74.2,10,5,,,WIFI",
    "66:77:88:99:AA:BB,NetTwo,[OPEN],2020-01-01,11,2462,-60,41.3,-75.4,12,6,,,WIFI",
]
_DIV_ROWS = [
    "epoch_ms,utc,date,lat,lon,alt,ssid,bssid,rssi",
    "1000,12:00:00,2020-01-01,40.1,-74.2,10,NetOne,00:11:22:33:44:55,-50",
    "2000,12:00:01,2020-01-01,41.3,-75.4,12,NetTwo,66:77:88:99:AA:BB,-60",
]

_EOLS = [("lf", "\n"), ("crlf", "\r\n"), ("cr", "\r")]


def _join(rows, eol):
    return eol.join(rows) + eol


@pytest.mark.parametrize("name,eol", _EOLS)
def test_iter_wigle_rows_handles_every_record_ending(name, eol):
    # CR-only used to raise csv.Error (new-line in unquoted field); now all three endings parse.
    rows = list(w.iter_wigle_rows(_join(_WIGLE_ROWS, eol)))
    assert [r["mac"] for r in rows] == ["00:11:22:33:44:55", "66:77:88:99:AA:BB"], name
    assert rows[0]["ssid"] == "NetOne" and rows[0]["lat"] == "40.1" and rows[0]["lon"] == "-74.2"


@pytest.mark.parametrize("name,eol", _EOLS)
def test_div_native_to_points_handles_every_record_ending(name, eol):
    # CR-only used to return [] (a valid file looked empty); now all three yield both points.
    pts = w.div_native_to_points(_join(_DIV_ROWS, eol))
    got = sorted(bssid for _, _, _, bssid in pts)
    assert got == ["00:11:22:33:44:55", "66:77:88:99:AA:BB"], name
    assert len(pts) == 2


def test_cr_only_wigle_no_longer_raises():
    # the exact witness: CR-only WiGLE must not raise (it used to raise csv.Error).
    rows = list(w.iter_wigle_rows(_join(_WIGLE_ROWS, "\r")))
    assert len(rows) == 2


def test_embedded_cr_inside_a_quoted_field_is_preserved():
    # newline="" must not globally strip CR: a CR inside a quoted field survives intact.
    rows = [
        "MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,CurrentLatitude,CurrentLongitude,"
        "AltitudeMeters,AccuracyMeters,RCOIs,MfgrId,Type",
        '00:11:22:33:44:55,"Net\rWithCR",[WPA2],2020-01-01,6,2437,-50,40.1,-74.2,10,5,,,WIFI',
    ]
    out = list(w.iter_wigle_rows("\r\n".join(rows) + "\r\n"))
    assert len(out) == 1
    assert out[0]["ssid"] == "Net\rWithCR"      # embedded CR retained, not split into a new record


def test_div_embedded_cr_inside_a_quoted_field_is_preserved():
    rows = [
        "epoch_ms,utc,date,lat,lon,alt,ssid,bssid,rssi",
        '1000,12:00:00,2020-01-01,40.1,-74.2,10,"Net\rWithCR",00:11:22:33:44:55,-50',
    ]
    pts = w.div_native_to_points("\r\n".join(rows) + "\r\n")
    assert len(pts) == 1
    assert pts[0][2] == "Net\rWithCR"           # ssid keeps its embedded CR
