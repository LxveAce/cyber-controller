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
