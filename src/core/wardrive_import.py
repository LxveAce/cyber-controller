"""Shared WiGLE-CSV read seam — the one place that turns WiGLE text into field-named rows.

Every WiGLE reader in the app (the summarizer, the map-point builder, the channel planner, and any
future importer) goes through :func:`iter_wigle_rows`, so they all handle the format the same way.

The point of the seam is version tolerance. The WigleWifi CSV ships in two column layouts we care about:

  * WigleWifi-1.4 (11 cols): MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,
    AltitudeMeters,AccuracyMeters,Type — what most ESP32/Marauder/M5 wardrivers and older Kismet emit.
  * WigleWifi-1.6 (14 cols): MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,CurrentLatitude,
    CurrentLongitude,AltitudeMeters,AccuracyMeters,RCOIs,MfgrId,Type — what the WiGLE app and current
    Kismet emit.

1.6 inserts a Frequency column at index 5, so RSSI and both coordinates shift one place right. A reader
that assumes one layout silently mis-reads the other — and this app used to require 14 columns outright,
so it dropped every row of a 1.4 file. The fix is to read the column-HEADER row and map each field by
NAME, so 1.4, 1.6, Kismet's .wiglecsv (either version), and reordered/extra columns all read correctly.
When there is no usable header (a hand-edited or synthetic file), we fall back to the 1.6 positions —
exactly what the callers assumed before, so their existing behaviour is unchanged.

Awareness-only: this reads already-captured broadcast metadata and transmits nothing.
"""
from __future__ import annotations

import csv
import io
from typing import Dict, Iterator, List, Optional

from src.core.wardrive import (
    _MAC_RE,  # a row is DATA iff field 0 is a colon MAC — reuse the one validator
)

# Canonical field -> its index in the WigleWifi-1.6 / 14-col layout. This is the positional FALLBACK used
# until (and unless) a real column-header row rebinds the mapping.
_DEFAULT_INDEX: Dict[str, int] = {
    "mac": 0, "ssid": 1, "auth": 2, "first_seen": 3, "channel": 4, "frequency": 5,
    "rssi": 6, "lat": 7, "lon": 8, "alt": 9, "accuracy": 10, "rcois": 11, "mfgrid": 12, "type": 13,
}

# WiGLE column-header name (upper, stripped, BOM-stripped) -> canonical field.
_HEADER_NAME: Dict[str, str] = {
    "MAC": "mac", "SSID": "ssid", "AUTHMODE": "auth", "FIRSTSEEN": "first_seen",
    "CHANNEL": "channel", "FREQUENCY": "frequency", "RSSI": "rssi",
    "CURRENTLATITUDE": "lat", "CURRENTLONGITUDE": "lon", "ALTITUDEMETERS": "alt",
    "ACCURACYMETERS": "accuracy", "RCOIS": "rcois", "MFGRID": "mfgrid", "TYPE": "type",
}

# A header row is only TRUSTED (name-mapped) if it resolves every field a caller keys on; otherwise it is
# junk that happens to share a token or two, and we keep the positional fallback.
_REQUIRED = ("mac", "channel", "rssi", "lat", "lon")

# The keys every yielded row carries (a field the active layout lacks — e.g. no Frequency in 1.4 — is "").
_YIELD = ("mac", "ssid", "auth", "first_seen", "channel", "rssi", "lat", "lon")


def _map_from_header(fields: List[str]) -> Optional[Dict[str, int]]:
    """Build a canonical-field -> column-index map from a WiGLE column-header row, or None if the row
    isn't a usable header (doesn't resolve all of ``_REQUIRED``). First occurrence wins on a duplicate."""
    idx: Dict[str, int] = {}
    for i, raw in enumerate(fields):
        canon = _HEADER_NAME.get(raw.strip().lstrip("﻿").upper())
        if canon is not None and canon not in idx:
            idx[canon] = i
    return idx if all(k in idx for k in _REQUIRED) else None


def iter_wigle_rows(text: str) -> Iterator[Dict[str, str]]:
    """Yield one dict per DATA row of a WiGLE CSV, keyed by canonical field name -> raw string value.

    Always yields exactly the ``_YIELD`` keys; a field the active layout doesn't carry comes back "".
    Callers keep their own ``int()``/``float()`` parsing and try/except, so tolerance is unchanged. The
    ``WigleWifi`` pre-header and the column-header row are skipped by the MAC-in-field-0 gate; a real
    column header re-binds the mapping — so a 1.4 file reads at 1.4 positions, a 1.6 file at 1.6
    positions, and a concatenated mixed-version file switches at each header. Short rows never raise; an
    out-of-range index just yields "".
    """
    idx = dict(_DEFAULT_INDEX)  # 1.6 positions until a usable header rebinds them
    first = True
    for row in csv.reader(io.StringIO(text)):
        if first:
            first = False
            if row and row[0][:1] == "﻿":  # strip a UTF-8 BOM off field 0 of the first line
                row[0] = row[0][1:]
        if not row:
            continue
        if _MAC_RE.fullmatch(row[0].strip()):  # a DATA row iff field 0 is a colon MAC
            yield {k: (row[idx[k]] if (idx.get(k) is not None and idx[k] < len(row)) else "")
                   for k in _YIELD}
            continue
        header = _map_from_header(row)  # otherwise it might be a (re-)binding column header
        if header is not None:
            idx = header
        # a pre-header line / junk / an unusable header: ignore it and keep the current mapping


def netxml_to_points(text: str) -> "list[tuple[float, float, str, str]]":
    """Parse a Kismet ``.netxml`` (its legacy XML export) into map points ``[(lat, lon, ssid, bssid)]``,
    one per network — the same 4-tuple shape :func:`~src.core.wardrive.wigle_csv_to_points` returns, so
    the map's AP layer plots both the same way.

    Mirrors the CSV path's guarantees: only a network with a real, in-range, non-Null-Island fix is
    emitted, deduped by BSSID. Kismet pre-aggregates each network's sightings, so the position is read
    from ``<gps-info>`` — preferring ``avg-`` (then ``peak-``, then ``min-``); a ``0/0`` aggregate is
    Kismet's no-fix sentinel and is skipped. Parsed with ``defusedxml`` (no external-entity or
    entity-expansion exposure). Tolerant: a malformed file yields ``[]`` rather than raising.
    Awareness-only: reads captured metadata, transmits nothing.
    """
    import math
    import re

    from defusedxml import ElementTree as ET  # safe parse: forbids entities + external resolution

    # ElementTree rejects a `str` still carrying an `<?xml encoding=...?>` declaration (the caller
    # already decoded the bytes, so the declared encoding is moot); drop the declaration so it parses.
    body = re.sub(r"<\?xml[^>]*\?>", "", text, count=1)
    try:
        root = ET.fromstring(body)
    except Exception:  # noqa: BLE001 — a malformed / hostile file imports as no points, never crashes
        return []

    best: Dict[str, tuple] = {}
    for net in root.iter("wireless-network"):
        bssid = (net.findtext("BSSID") or "").strip()
        if not _MAC_RE.fullmatch(bssid):
            continue
        gps = net.find("gps-info")
        if gps is None:
            continue
        lat: Optional[float] = None
        lon: Optional[float] = None
        for la, lo in (("avg-lat", "avg-lon"), ("peak-lat", "peak-lon"), ("min-lat", "min-lon")):
            a, o = gps.findtext(la), gps.findtext(lo)
            if not (a and o):
                continue
            try:
                fa, fo = float(a), float(o)
            except ValueError:
                continue
            if fa == 0.0 and fo == 0.0:
                continue  # Kismet's no-fix sentinel for this aggregate — try the next one
            lat, lon = fa, fo
            break
        if lat is None or lon is None:
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        b = bssid.upper()
        ssid = (net.findtext("SSID/essid") or "").strip()
        best.setdefault(b, (lat, lon, ssid, b))  # one point per network (Kismet pre-aggregates)
    return list(best.values())


def sniff_wardrive_format(text: str) -> str:
    """Best-effort format of a wardrive log by its content: ``"netxml"`` | ``"wigle"`` | ``"unknown"``.

    Content, not extension — the operator's file may be misnamed, and Biscuit/Kismet both export plain
    WiGLE CSVs. Kismet ``.netxml`` is XML (``<detection-run>`` / ``<wireless-network>``); a WiGLE CSV
    opens with a ``WigleWifi-`` pre-header or a MAC-first column-header / data row.
    """
    head = text[:4096]
    if "<detection-run" in head or "<wireless-network" in head:
        return "netxml"
    for line in io.StringIO(head):
        s = line.strip().lstrip("﻿").strip()
        if not s:
            continue
        if s.startswith("WigleWifi-"):
            return "wigle"
        first = s.split(",", 1)[0].strip().strip('"')
        if first.upper() == "MAC" or _MAC_RE.fullmatch(first):
            return "wigle"
        break  # the first non-empty line settles it
    return "unknown"


def wardrive_points(text: str) -> "list[tuple[float, float, str, str]]":
    """Import ANY supported wardrive log — a WiGLE CSV (incl. Biscuit + Kismet ``.wiglecsv``) or a Kismet
    ``.netxml`` — into map points ``[(lat, lon, ssid, bssid)]``, dispatching by a content sniff. One entry
    point for the file-import UI; an unrecognized file yields ``[]`` (never raises). Awareness-only.
    """
    from src.core.wardrive import wigle_csv_to_points  # lazy: keeps the module import acyclic

    if sniff_wardrive_format(text) == "netxml":
        return netxml_to_points(text)
    # WiGLE CSV is the tolerant default (Biscuit + Kismet .wiglecsv ride it, and it already returns []
    # for anything unparseable — so a stray non-log file imports as no points, never raises).
    return wigle_csv_to_points(text)
