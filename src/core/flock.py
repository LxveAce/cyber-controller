"""Flock-You session (FL F2) — GPS-fused ALPR-camera detection log.

Mirrors :class:`~src.core.wardrive.WardriveSession`: feed GPS NMEA lines + Flock-You serial lines; every ALPR
detection that arrives while a GPS fix is held is stamped with the fix's lat/lon/utc, de-duplicated by MAC
(strongest RSSI wins — the location observed at the strongest signal, exactly like WardriveSession keeps the
strongest-RSSI row), and emitted as a portable **cameras GeoJSON FeatureCollection** — the offline detection
log the heatmap (F4) and the mobile view render, and the shareable artifact of a Flock scan.

Passive + awareness-only: like the Flock-You protocol itself, this records WHERE surveillance cameras were
seen and never emits any attack/action. GPS is host-side (the firmware is a receive-only 2.4 GHz sniffer), so
``parse_nmea`` is reused verbatim from the wardrive rig.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, TextIO

from src.core.wardrive import GpsFix, parse_nmea
from src.protocols.flock_you import FlockYouProtocol


def _utcnow() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _as_int(value: object) -> int:
    try:
        return int(value)  # ev.data ints arrive pre-coerced; this is a defensive fallback
    except (TypeError, ValueError):
        return 0


def _signal_key(rssi: int) -> int:
    """Comparison key for 'strongest signal'. RSSI 0 is the parser's missing/unknown sentinel (a real ALPR
    reading is strongly negative — 0 dBm at the antenna is physically absurd), so it must rank BELOW any real
    reading, else a drifted no-rssi detection would win the dedup and hijack the camera's mapped location."""
    return rssi if rssi != 0 else -9999


@dataclass
class CameraDetection:
    """One de-duplicated Flock ALPR camera, located at the strongest-RSSI GPS fix seen for it."""
    mac: str
    lat: float
    lon: float
    ssid: str = ""
    oui: str = ""
    detection_method: str = ""
    rssi: int = 0
    channel: int = 0
    frequency: int = 0
    utc: str = ""            # NMEA UTC of the strongest-RSSI fix
    first_seen: str = ""
    last_seen: str = ""
    count: int = 0

    def to_feature(self) -> dict:
        # GeoJSON coordinates are [lon, lat] (x, y) — not lat/lon.
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(self.lon, 6), round(self.lat, 6)]},
            "properties": {
                "mac": self.mac,
                "ssid": self.ssid,
                "oui": self.oui,
                "detection_method": self.detection_method,
                "rssi": self.rssi,
                "channel": self.channel,
                "frequency": self.frequency,
                "utc": self.utc,
                "first_seen": self.first_seen,
                "last_seen": self.last_seen,
                "count": self.count,
            },
        }


@dataclass
class FlockSession:
    """Drive a Flock scan: feed GPS + Flock-You lines; accumulate located, deduped ALPR cameras."""
    fix: Optional[GpsFix] = None
    cameras: Dict[str, CameraDetection] = field(default_factory=dict)
    _parser: FlockYouProtocol = field(default_factory=FlockYouProtocol, repr=False, compare=False)

    def update_gps(self, line: str) -> Optional[GpsFix]:
        """Feed one NMEA line; on a parseable sentence, becomes the current fix. Returns it (or None).

        NOTE the fix is *sticky*: it persists until the next parseable sentence, so a detection is stamped
        with the most recent fix even if the GPS has since moved or gone silent (same behavior as the
        wardrive rig). A sentence that parses to a no-fix (quality 0) correctly disables recording.
        """
        if line is None:
            return None
        f = parse_nmea(line)
        if f is not None:
            self.fix = f
        return f

    @property
    def has_fix(self) -> bool:
        return bool(self.fix and self.fix.has_fix)

    def observe(self, line: str, now: Optional[str] = None) -> bool:
        """Parse a Flock-You line; if it is an ALPR detection with a real MAC AND we hold a GPS fix, record or
        refresh a located camera. Returns True iff a camera was ADDED or relocated to a stronger fix.

        De-duplicates by MAC keeping the strongest RSSI (and the location observed at that strongest RSSI) —
        the same rule WardriveSession uses for BSSIDs. A detection with no MAC or no current fix is dropped
        (it cannot be placed on the map), mirroring the wardrive rig's fix-required behavior.
        """
        ev = self._parser.parse_line(line)
        if ev is None or ev.event_type != "alpr_found":
            return False
        mac = str(ev.data.get("mac") or "").strip()
        if not mac or not self.has_fix:
            return False
        assert self.fix is not None  # has_fix guarantees this
        now = now or _utcnow()
        rssi = _as_int(ev.data.get("rssi"))
        prev = self.cameras.get(mac)
        if prev is None:
            self.cameras[mac] = CameraDetection(
                mac=mac,
                lat=self.fix.lat,
                lon=self.fix.lon,
                ssid=str(ev.data.get("ssid") or ""),
                oui=str(ev.data.get("oui") or ""),
                detection_method=str(ev.data.get("detection_method") or ""),
                rssi=rssi,
                channel=_as_int(ev.data.get("channel")),
                frequency=_as_int(ev.data.get("frequency")),
                utc=self.fix.utc,
                first_seen=now,
                last_seen=now,
                count=1,
            )
            return True
        # Seen before: always bump count + last_seen; relocate/refresh only on a STRICTLY stronger REAL RSSI
        # (a missing-rssi 0 sentinel must not hijack the location — see _signal_key).
        prev.count += 1
        prev.last_seen = now
        if _signal_key(rssi) > _signal_key(prev.rssi):
            prev.rssi = rssi
            prev.lat, prev.lon, prev.utc = self.fix.lat, self.fix.lon, self.fix.utc
            prev.channel = _as_int(ev.data.get("channel"))
            prev.frequency = _as_int(ev.data.get("frequency"))
            # Keep the richest identity fields when the stronger line carries them.
            if ev.data.get("ssid"):
                prev.ssid = str(ev.data["ssid"])
            if ev.data.get("oui"):
                prev.oui = str(ev.data["oui"])
            if ev.data.get("detection_method"):
                prev.detection_method = str(ev.data["detection_method"])
            return True
        return False

    @property
    def camera_count(self) -> int:
        return len(self.cameras)

    def to_geojson(self) -> dict:
        """A GeoJSON FeatureCollection of the located cameras, sorted by MAC (deterministic output)."""
        return {
            "type": "FeatureCollection",
            "features": [c.to_feature() for c in sorted(self.cameras.values(), key=lambda c: c.mac)],
        }

    def write_geojson(self, out: TextIO) -> int:
        """Write the camera FeatureCollection as GeoJSON to *out*; returns the camera count written."""
        json.dump(self.to_geojson(), out, indent=2)
        out.flush()
        return len(self.cameras)
