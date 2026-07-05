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
            has_fix = bool(fix_q) and fix_q != "0" and lat is not None and lon is not None
            if lat is None or lon is None:
                return GpsFix(0.0, 0.0, alt, False, utc)
            return GpsFix(lat, lon, alt, has_fix, utc)
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


def parse_marauder_ap(line: str) -> Optional[ApObservation]:
    """Tolerantly parse one ESP32/Marauder AP scan line into an :class:`ApObservation`.

    Requires a BSSID (MAC). RSSI/channel/SSID/encryption are extracted if present (formats vary across
    Marauder versions, so this is field-extraction rather than a fixed column parse).
    """
    m = _MAC_RE.search(line)
    if not m:
        return None
    bssid = m.group(1).lower()
    rssi = 0
    rm = re.search(r"RSSI[:=]?\s*(-?\d+)", line, re.I)
    if rm:
        rssi = int(rm.group(1))
    ch = 0
    cm = re.search(r"\bCh(?:annel)?[:=]?\s*(\d+)", line, re.I)
    if cm:
        ch = int(cm.group(1))
    ssid = ""
    sm = re.search(r"\bE?SSID[:=]?\s*(.+?)\s*(?:\||$)", line, re.I)  # \b so 'BSSID' is not matched
    if sm:
        ssid = sm.group(1).strip()
    auth = "[ESS]"
    am = re.search(r"\b(WPA3|WPA2|WPA|WEP|OPEN)\b", line, re.I)
    if am:
        tok = am.group(1).upper()
        auth = "[ESS]" if tok == "OPEN" else f"[{tok}][ESS]"
    return ApObservation(bssid=bssid, ssid=ssid, channel=ch, rssi=rssi, auth=auth)


# ── WiGLE CSV row ────────────────────────────────────────────────────

def _csv_field(s: str) -> str:
    s = "" if s is None else str(s)
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
        obs = parse_marauder_ap(line)
        if obs is None or not self.has_fix:
            return False
        prev = self.seen.get(obs.bssid)
        if prev is not None and obs.rssi <= prev:
            return False  # already logged with an equal/stronger signal
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
        obs = parse_marauder_ap(line)
        if obs is None or not self.has_fix:
            return False
        prev = self.seen.get(obs.bssid)
        if prev is not None and obs.rssi <= prev:
            return False
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
