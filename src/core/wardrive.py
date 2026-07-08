"""Wardriving subsystem — GPS-tagged Wi-Fi capture exported as WiGLE CSV.

LAWFUL, OWNER-AUTHORIZED USE ONLY. This logs *broadcast* Wi-Fi beacon metadata (SSID/BSSID/channel/
signal that every AP transmits openly) tagged with your own GPS position — the same passive activity as
WiGLE's app and ESP32 Marauder / Biscuit wardrive mode. It does NOT deauth, capture handshakes, or
touch traffic. Passive beacon wardriving is generally lawful in the US; you are responsible for local
law and for only operating equipment you own/are authorized to use.

Pipeline (mirrors the Marauder/Biscuit flow):
  * Parse GPS **NMEA** (GGA/RMC) for a position; a row is written ONLY when there is a valid fix
    (matching Marauder's "No Fix" gating — no fix, no row).
  * Parse the ESP32 serial scan output for access points.
  * Emit **WiGLE CSV** (``WigleWifi-1.6`` pre-header + the standard 14-column row) for upload to
    wigle.net, de-duplicated by BSSID (strongest RSSI kept).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, TextIO

WIGLE_HEADER = ("MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,CurrentLatitude,"
                "CurrentLongitude,AltitudeMeters,AccuracyMeters,RCOIs,MfgrId,Type")


def wigle_preheader(app_version: str = "1.0") -> str:
    return (f"WigleWifi-1.6,appRelease={app_version},model=CyberController,release={app_version},"
            "device=cyber-controller,display=,board=,brand=LxveAce,star=Sol,body=3,subBody=0")


# ── data ─────────────────────────────────────────────────────────────

@dataclass
class GpsFix:
    lat: float
    lon: float
    alt: float = 0.0
    has_fix: bool = False
    utc: str = ""
    sats: int = 0        # satellites in use (GGA field 7); 0 when unknown (e.g. an RMC-only fix)
    hdop: float = 0.0    # horizontal dilution of precision (GGA field 8); 0.0 when unknown


@dataclass
class ApObservation:
    bssid: str
    ssid: str = ""
    channel: int = 0
    rssi: int = 0
    auth: str = "[ESS]"
    kind: str = "WIFI"


# ── NMEA GPS parsing ─────────────────────────────────────────────────

def _dm_to_dd(dm: str, hemi: str) -> Optional[float]:
    """Convert an NMEA ddmm.mmmm / dddmm.mmmm value + hemisphere to signed decimal degrees."""
    if not dm:
        return None
    try:
        val = float(dm)
    except ValueError:
        return None
    deg = int(val // 100)
    minutes = val - deg * 100
    dd = deg + minutes / 60.0
    if hemi.upper() in ("S", "W"):
        dd = -dd
    return dd


def parse_nmea(line: str) -> Optional[GpsFix]:
    """Parse a GGA or RMC NMEA sentence into a :class:`GpsFix`, or None if not parseable.

    Accepts any talker id (GP/GN/GL/...). ``has_fix`` reflects GGA fix-quality > 0 or RMC status 'A'.
    """
    line = line.strip()
    if not line.startswith("$"):
        return None
    body = line[1:].split("*", 1)[0]
    parts = body.split(",")
    if not parts or len(parts[0]) < 5:
        return None
    kind = parts[0][2:]  # strip talker id (GP/GN/...)
    try:
        if kind == "GGA" and len(parts) >= 10:
            utc = parts[1]
            lat = _dm_to_dd(parts[2], parts[3])
            lon = _dm_to_dd(parts[4], parts[5])
            fix_q = parts[6]
            alt = float(parts[9]) if parts[9] else 0.0
            # Satellites (field 7) + HDOP (field 8) are guarded individually: a garbled quality field must
            # never discard an otherwise-valid position fix, only leave the quality figure unknown (0).
            try:
                sats = int(parts[7]) if parts[7] else 0
            except ValueError:
                sats = 0
            try:
                hdop = float(parts[8]) if parts[8] else 0.0
            except ValueError:
                hdop = 0.0
            has_fix = bool(fix_q) and fix_q != "0" and lat is not None and lon is not None
            if lat is None or lon is None:
                return GpsFix(0.0, 0.0, alt, False, utc, sats, hdop)
            return GpsFix(lat, lon, alt, has_fix, utc, sats, hdop)
        if kind == "RMC" and len(parts) >= 7:
            utc = parts[1]
            status = parts[2]
            lat = _dm_to_dd(parts[3], parts[4])
            lon = _dm_to_dd(parts[5], parts[6])
            has_fix = status.upper() == "A" and lat is not None and lon is not None
            if lat is None or lon is None:
                return GpsFix(0.0, 0.0, 0.0, False, utc)
            return GpsFix(lat, lon, 0.0, has_fix, utc)
    except (ValueError, IndexError):
        return None
    return None


# ── channel / frequency ──────────────────────────────────────────────

def channel_to_frequency(ch: int) -> int:
    """Wi-Fi channel -> centre frequency in MHz (2.4 GHz + common 5 GHz)."""
    if ch <= 0:
        return 0
    if ch == 14:
        return 2484
    if 1 <= ch <= 13:
        return 2407 + ch * 5
    return 5000 + ch * 5  # 5 GHz (ch 32..177)


# ── Marauder/ESP32 scan-line parsing ─────────────────────────────────

_MAC_RE = re.compile(r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})")
_RSSI_RE = re.compile(r"RSSI[:=]?\s*(-?\d+)", re.I)
_CH_RE = re.compile(r"\bCh(?:annel)?[:=]?\s*(\d+)", re.I)
# SSID capture is BOUNDED: the non-greedy value stops at the next key token (BSSID/SSID/Ch/RSSI) or a
# '|' delimiter, not just at end-of-line. Without that bound a space-separated single-line record with
# no pipe (the legacy Marauder form 'SSID: MyNet BSSID: .. Ch: .. RSSI: ..') lets the group run to EOL
# and swallow the trailing fields into the SSID. Leading \b keeps 'BSSID' from matching as an 'SSID'.
_SSID_RE = re.compile(
    r"\bE?SSID[:=]?\s*(.+?)(?:\s*\||\s+(?:B?SSID|Ch(?:annel)?|RSSI)[:=]|\s*$)", re.I
)
_AUTH_RE = re.compile(r"\b(WPA3|WPA2|WPA|WEP|OPEN)\b", re.I)


def _extract_ap_fields(line: str) -> Dict[str, object]:
    """Extract whatever AP fields appear on ONE serial line.

    Returns a dict holding only the keys found among ``bssid`` / ``rssi`` / ``channel`` / ``ssid`` /
    ``auth``. Shared by the single-line :func:`parse_marauder_ap` and the multi-line
    :class:`_ApAccumulator` so both read a given field identically.
    """
    fields: Dict[str, object] = {}
    m = _MAC_RE.search(line)
    if m:
        fields["bssid"] = m.group(1).lower()
    rm = _RSSI_RE.search(line)
    if rm:
        fields["rssi"] = int(rm.group(1))
    cm = _CH_RE.search(line)
    if cm:
        fields["channel"] = int(cm.group(1))
    sm = _SSID_RE.search(line)
    if sm:
        fields["ssid"] = sm.group(1).strip()
    am = _AUTH_RE.search(line)
    if am:
        tok = am.group(1).upper()
        fields["auth"] = "[ESS]" if tok == "OPEN" else f"[{tok}][ESS]"
    return fields


def _obs_from_fields(fields: Dict[str, object]) -> ApObservation:
    return ApObservation(
        bssid=str(fields["bssid"]),
        ssid=str(fields.get("ssid", "")),
        channel=int(fields.get("channel", 0)),   # type: ignore[arg-type]
        rssi=int(fields.get("rssi", 0)),          # type: ignore[arg-type]
        auth=str(fields.get("auth", "[ESS]")),
    )


def parse_marauder_ap(line: str) -> Optional[ApObservation]:
    """Tolerantly parse ONE self-contained ESP32/Marauder AP scan line into an :class:`ApObservation`.

    Requires a BSSID (MAC); RSSI/channel/SSID/encryption are extracted if present (formats vary across
    Marauder versions, so this is field-extraction rather than a fixed column parse). This is the
    single-line path — modern Marauder (v1.12.3+) streams each AP across SEPARATE lines, which the
    session reassembles via :class:`_ApAccumulator`.
    """
    fields = _extract_ap_fields(line)
    if "bssid" not in fields:
        return None
    return _obs_from_fields(fields)


class _ApAccumulator:
    """Reassemble one AP from either a single line or several consecutive lines.

    Modern Marauder ``scanall`` (v1.12.3+) prints each AP across SEPARATE ``ESSID:`` / ``BSSID:`` /
    ``RSSI:`` (+ optional ``Ch:``) lines, so a stateless single-line parser drops every field except the
    one on the BSSID line — logging an AP with a blank SSID and 0 channel/RSSI. This accumulator stitches
    those fragments into one record and emits an :class:`ApObservation` only once BOTH a BSSID and an
    RSSI have been seen. A complete single-line record (legacy Marauder / GhostESP pipe form) carries all
    fields at once and so emits immediately.

    An ``ESSID``/``SSID`` token starts a fresh record (it is the first line Marauder prints per AP), so a
    new AP never inherits the previous one's fields. Feed exactly the lines from ONE serial stream;
    interleaved streams (several boards) need one accumulator each.
    """

    def __init__(self) -> None:
        self._record: Optional[Dict[str, object]] = None

    def feed(self, line: str) -> Optional[ApObservation]:
        fields = _extract_ap_fields(line)
        if not fields:
            return None
        if "ssid" in fields or self._record is None:
            # A new SSID line — or the first fragment seen — begins a fresh AP record.
            self._record = {}
        self._record.update(fields)
        if "bssid" in self._record and "rssi" in self._record:
            obs = _obs_from_fields(self._record)
            self._record = None
            return obs
        return None


# ── WiGLE CSV row ────────────────────────────────────────────────────

# Leading characters a spreadsheet (Excel / LibreOffice Calc) treats as the start of a *formula*.
# An untrusted free-text field (e.g. an attacker-chosen Wi-Fi SSID) beginning with one of these is
# evaluated on open — enabling DDE/command execution or data exfiltration — even when the value contains
# none of the RFC-4180 delimiters, so quoting alone does NOT stop it. See OWASP "CSV Injection".
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_field(s: str) -> str:
    s = "" if s is None else str(s)
    # Neutralize spreadsheet formula injection before delimiter-quoting: prefix a leading formula
    # trigger with a single quote so Calc/Excel render it as literal text (the SSID stays readable for
    # a wigle.net upload). Done first so a value like "\r=cmd" is both de-fanged and then CR-quoted.
    if s and s[0] in _FORMULA_PREFIXES:
        s = "'" + s
    if any(c in s for c in ',"\n\r'):
        return '"' + s.replace('"', '""') + '"'
    return s


def to_wigle_row(obs: ApObservation, fix: GpsFix, first_seen: str) -> str:
    return ",".join([
        obs.bssid.upper(),
        _csv_field(obs.ssid),
        _csv_field(obs.auth),
        first_seen,
        str(obs.channel),
        str(channel_to_frequency(obs.channel)),
        str(obs.rssi),
        f"{fix.lat:.6f}",
        f"{fix.lon:.6f}",
        f"{fix.alt:.1f}",
        "0",          # AccuracyMeters (unknown from beacon-only capture)
        "",           # RCOIs
        "",           # MfgrId
        obs.kind,
    ])


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _signal_key(rssi: int) -> int:
    """Comparison key for 'strongest signal'. RSSI 0 is the parser's missing/unknown sentinel
    (:func:`parse_marauder_ap` leaves ``rssi=0`` when a line has no ``RSSI`` token, and real Wi-Fi RSSI is
    strongly negative — 0 dBm at the antenna is physically absurd), so it must rank BELOW any real reading;
    otherwise a no-RSSI sighting (0) would beat a genuine strong reading (e.g. -40) and hijack the mapped
    location. Mirrors :func:`src.core.flock._signal_key`."""
    return rssi if rssi != 0 else -9999


# ── session ──────────────────────────────────────────────────────────

@dataclass
class WardriveSession:
    """Drive a wardrive run: feed GPS lines + scan lines; writes deduped WiGLE rows on a valid fix."""
    out: TextIO
    app_version: str = "1.0"
    fix: Optional[GpsFix] = None
    ap_count: int = 0
    seen: Dict[str, int] = field(default_factory=dict)  # bssid -> best RSSI
    _header_written: bool = False
    _parser: _ApAccumulator = field(default_factory=_ApAccumulator)  # stitches multi-line AP records

    def start(self) -> None:
        self.out.write(wigle_preheader(self.app_version) + "\n")
        self.out.write(WIGLE_HEADER + "\n")
        self.out.flush()
        self._header_written = True

    def update_gps(self, line: str) -> Optional[GpsFix]:
        f = parse_nmea(line)
        if f is not None:
            self.fix = f
        return f

    @property
    def has_fix(self) -> bool:
        return bool(self.fix and self.fix.has_fix)

    def observe(self, line: str, now: Optional[str] = None) -> bool:
        """Parse a scan line; if it is an AP and we have a GPS fix, write/refresh a WiGLE row.

        Returns True iff a row was written. De-duplicates by BSSID, keeping the strongest RSSI.
        """
        if not self._header_written:
            self.start()
        obs = self._parser.feed(line)
        if obs is None or not self.has_fix:
            return False
        prev = self.seen.get(obs.bssid)
        if prev is not None and _signal_key(obs.rssi) <= _signal_key(prev):
            return False  # already logged with an equal/stronger signal (a missing-RSSI 0 can't overwrite)
        self.seen[obs.bssid] = obs.rssi
        if prev is None:
            self.ap_count += 1
        self.out.write(to_wigle_row(obs, self.fix, now or _now()) + "\n")
        self.out.flush()
        return True


@dataclass
class MultiWardriveSession:
    """One wardrive run across MANY boards that share a single GPS feed and one merged WiGLE CSV (F1).

    Most decks have one GPS receiver feeding several capture boards, so this owns ONE :class:`GpsFix` and
    fans it out to every board's AP stream. Observations are de-duplicated into one shared BSSID set
    (strongest RSSI wins), producing a single combined map/CSV. Per-board counts track each board's
    first-seen contribution and ``ap_count`` is the number of unique APs. The output stays standard WiGLE
    (uploadable) — source-port attribution lives in :attr:`per_board`, not in the CSV. Pure: no Qt, no serial.
    """
    out: TextIO
    app_version: str = "1.0"
    fix: Optional[GpsFix] = None
    seen: Dict[str, int] = field(default_factory=dict)          # bssid -> best RSSI (shared across boards)
    per_board: Dict[str, int] = field(default_factory=dict)     # port -> unique APs first seen by that board
    _header_written: bool = False
    # One AP accumulator PER port: boards stream concurrently, so a shared accumulator would interleave
    # different boards' multi-line ESSID/BSSID/RSSI fragments into corrupt records.
    _parsers: Dict[str, _ApAccumulator] = field(default_factory=dict)

    def start(self) -> None:
        self.out.write(wigle_preheader(self.app_version) + "\n")
        self.out.write(WIGLE_HEADER + "\n")
        self.out.flush()
        self._header_written = True

    def add_board(self, port: str) -> None:
        """Register a board so it appears in :attr:`per_board` even before it contributes an AP."""
        self.per_board.setdefault(port, 0)

    def update_gps(self, line: str) -> Optional[GpsFix]:
        """Feed one NMEA line from the SHARED GPS; the resulting fix gates every board's rows."""
        f = parse_nmea(line)
        if f is not None:
            self.fix = f
        return f

    @property
    def has_fix(self) -> bool:
        return bool(self.fix and self.fix.has_fix)

    @property
    def ap_count(self) -> int:
        return len(self.seen)                                   # unique APs across every board

    def observe(self, port: str, line: str, now: Optional[str] = None) -> bool:
        """Feed one scan line from *port*. On a valid shared fix, write/refresh a merged WiGLE row.

        Returns True iff a row was written. De-duplicates by BSSID across ALL boards, keeping the strongest
        RSSI; the board that first sees a BSSID gets the per-board credit (a stronger re-sighting by another
        board refreshes the row but is not double-counted).
        """
        if not self._header_written:
            self.start()
        self.per_board.setdefault(port, 0)
        obs = self._parsers.setdefault(port, _ApAccumulator()).feed(line)
        if obs is None or not self.has_fix:
            return False
        prev = self.seen.get(obs.bssid)
        if prev is not None and _signal_key(obs.rssi) <= _signal_key(prev):
            return False  # a missing-RSSI 0 must not overwrite a real reading's merged row/location
        if prev is None:
            self.per_board[port] += 1
        self.seen[obs.bssid] = obs.rssi
        self.out.write(to_wigle_row(obs, self.fix, now or _now()) + "\n")
        self.out.flush()
        return True


# ── per-firmware scan commands (F1 slice 3) ──────────────────────────

@dataclass(frozen=True)
class ScanCommands:
    """The native wardrive scan commands + line terminator for one firmware."""
    start: tuple                # commands to send, in order, to begin an AP scan (pre-commands + the verb)
    stop: str                   # command that halts the scan
    line_ending: str            # terminator the firmware's shell expects (LF for most, CR for Flipper, …)


# Legacy Marauder-style fallback, used when the firmware is unknown (Device.firmware often isn't set until a
# board is identified in the Devices tab). "scanap" logs AP beacons only — exactly what a WiGLE run wants.
_DEFAULT_SCAN = ScanCommands(("scanap",), "stopscan", "\n")


def scan_commands_for(firmware: str) -> ScanCommands:
    """Resolve the scan start/stop commands + line terminator for *firmware*.

    Reuses the per-firmware ``BROADCAST_CAPABILITIES`` table (FIND_APS / STOP_ALL) so a mixed deck
    (Marauder, GhostESP, Flock-You, HaleHound, …) each gets its OWN native verb instead of a hardcoded
    ``scanap`` that only Marauder understands — that mismatch is the bug this fixes. Unknown or
    capability-less firmware falls back to the Marauder default. Pure: the protocol/broadcast imports are
    lazy so this module stays importable on its own.
    """
    fw = (firmware or "").strip()
    if not fw:
        return _DEFAULT_SCAN
    try:
        from src.core.broadcast import BroadcastVerb
        from src.protocols import get_protocol_module, line_ending_for
    except Exception:  # noqa: BLE001 — resolver deps unavailable -> safe default
        return _DEFAULT_SCAN
    mod = get_protocol_module(fw)
    caps = getattr(mod, "BROADCAST_CAPABILITIES", {}) if mod else {}
    try:
        line_ending = line_ending_for(fw) or "\n"
    except Exception:  # noqa: BLE001
        line_ending = "\n"
    start, stop = _DEFAULT_SCAN.start, _DEFAULT_SCAN.stop
    find = caps.get(BroadcastVerb.FIND_APS)
    if find is not None:
        pre, cmd = find
        start = tuple(pre) + (cmd,)
    stop_cap = caps.get(BroadcastVerb.STOP_ALL)
    if stop_cap is not None:
        stop = stop_cap[1]
    return ScanCommands(start, stop, line_ending)
