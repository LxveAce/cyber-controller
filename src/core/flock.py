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
import os
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, Optional, TextIO, Union

from src.core.wardrive import GpsFix, parse_nmea
from src.protocols.flock_you import FlockYouProtocol


def _utcnow() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _as_int(value: object) -> int:
    try:
        return int(value)  # ev.data ints arrive pre-coerced; this is a defensive fallback
    except (TypeError, ValueError, OverflowError):
        # OverflowError (NOT a ValueError subclass) fires on int(inf)/int(nan) — e.g. a JSON
        # "rssi": 1e400 parsed to float('inf'); coerce to the 0 sentinel rather than propagate.
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


_CSV_COLUMNS = ("mac", "lat", "lon", "ssid", "oui", "detection_method", "rssi",
                "channel", "frequency", "utc", "first_seen", "last_seen", "count")
_CSV_NUMERIC = frozenset({"rssi", "channel", "frequency", "count"})


def cameras_geojson_to_csv(geojson: dict) -> str:
    """Render a located-camera GeoJSON FeatureCollection (as produced by :meth:`FlockSession.to_geojson`,
    or loaded from a saved ``cameras.geojson``) as a spreadsheet-friendly CSV — one row per camera, lat/lon
    pulled out of the GeoJSON ``[lon, lat]`` geometry. Robust to a missing/odd shape: non-dict features and
    features with no usable coordinate pair are skipped, missing properties become blank.

    ``ssid``/``oui`` are UNTRUSTED broadcast strings, so every TEXT field is neutralized against spreadsheet
    formula injection via the shared :func:`wardrive._csv_field` (OWASP CSV-injection). Numeric fields are
    emitted as-is — routing them through ``_csv_field`` would quote-prefix a legitimate negative RSSI.
    """
    from src.core.wardrive import _csv_field
    rows = [",".join(_CSV_COLUMNS)]
    feats = geojson.get("features") if isinstance(geojson, dict) else None
    for feat in feats or []:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") if isinstance(geom, dict) else None
        if not (isinstance(coords, (list, tuple)) and len(coords) >= 2):
            continue  # a camera with no location isn't a map/CSV row
        lon, lat = coords[0], coords[1]
        props = feat.get("properties")
        props = props if isinstance(props, dict) else {}
        cells = []
        for col in _CSV_COLUMNS:
            if col == "lat":
                cells.append(f"{lat:.6f}" if isinstance(lat, (int, float)) and not isinstance(lat, bool) else "")
            elif col == "lon":
                cells.append(f"{lon:.6f}" if isinstance(lon, (int, float)) and not isinstance(lon, bool) else "")
            elif col in _CSV_NUMERIC:
                v = props.get(col, "")
                cells.append(str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else "")
            else:  # untrusted text (mac/ssid/oui/detection_method/utc/first_seen/last_seen)
                cells.append(_csv_field(str(props.get(col, ""))))
        rows.append(",".join(cells))
    return "\n".join(rows) + "\n"


def _merge_camera(a: CameraDetection, b: CameraDetection) -> CameraDetection:
    """Merge two observations of the SAME camera (same MAC) into one, using the exact dedup rule
    ``FlockSession.observe`` uses: the location + rssi + channel/frequency come from the STRONGER real
    RSSI reading (a missing-rssi 0 sentinel loses — see :func:`_signal_key`), the counts add up, and the
    seen-window widens (earliest ``first_seen``, latest ``last_seen``). Identity fields (ssid/oui/method)
    prefer the stronger reading, then any non-empty value. Symmetric except an exact RSSI tie, which keeps
    *a*'s location.
    """
    strong, weak = (a, b) if _signal_key(a.rssi) >= _signal_key(b.rssi) else (b, a)
    firsts = [s for s in (a.first_seen, b.first_seen) if s]
    lasts = [s for s in (a.last_seen, b.last_seen) if s]
    return CameraDetection(
        mac=strong.mac,
        lat=strong.lat, lon=strong.lon, utc=strong.utc,
        rssi=strong.rssi, channel=strong.channel, frequency=strong.frequency,
        ssid=strong.ssid or weak.ssid,
        oui=strong.oui or weak.oui,
        detection_method=strong.detection_method or weak.detection_method,
        first_seen=min(firsts) if firsts else "",
        last_seen=max(lasts) if lasts else "",
        count=a.count + b.count,
    )


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

    def checkpoint(self, path: Union[str, Path]) -> int:
        """Atomically persist the current cameras as GeoJSON to *path*.

        Call it after each add (``observe()`` returned True) so a crash mid-drive can't
        lose the run — this replaces the one-shot :meth:`write_geojson` for live recording.
        Writes a temp file in the same directory then ``os.replace``s it into place, so a
        reader (or a resume after a crash) never sees a half-written file. Returns the
        camera count written.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self.to_geojson(), indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return len(self.cameras)

    @classmethod
    def from_checkpoint(cls, path: Union[str, Path]) -> "FlockSession":
        """Rebuild a session's cameras from a GeoJSON checkpoint written by :meth:`checkpoint`.

        Use it to resume a drive after a restart, or to reload a saved scan. A missing or
        malformed file yields an empty session (never raises), and any individual feature
        that can't be parsed is skipped rather than sinking the whole load. The GPS ``fix``
        is intentionally NOT restored — a resumed drive re-acquires its own fix.
        """
        session = cls()
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return session
        # `features` must be a list; a truthy non-iterable (a stray number/bool from a garbage-written
        # file) would otherwise raise here and break the "never raises" resume contract.
        feats = raw.get("features") if isinstance(raw, dict) else None
        for feat in feats if isinstance(feats, list) else []:
            try:
                lon, lat = feat["geometry"]["coordinates"][:2]
                props = feat.get("properties") or {}
                mac = str(props.get("mac") or "").strip()
                if not mac:
                    continue
                session.cameras[mac] = CameraDetection(
                    mac=mac,
                    lat=float(lat),
                    lon=float(lon),
                    ssid=str(props.get("ssid") or ""),
                    oui=str(props.get("oui") or ""),
                    detection_method=str(props.get("detection_method") or ""),
                    rssi=_as_int(props.get("rssi")),
                    channel=_as_int(props.get("channel")),
                    frequency=_as_int(props.get("frequency")),
                    utc=str(props.get("utc") or ""),
                    first_seen=str(props.get("first_seen") or ""),
                    last_seen=str(props.get("last_seen") or ""),
                    count=_as_int(props.get("count")),
                )
            except (KeyError, TypeError, ValueError, IndexError, OverflowError):
                # OverflowError isn't a ValueError subclass (sibling under ArithmeticError). It
                # fires on a giant-int coordinate -> float(), or an infinite property -> int(inf).
                # Skip the one bad feature rather than sink the whole never-raises resume.
                continue
        return session

    def merge(self, other: "Union[FlockSession, Dict[str, CameraDetection]]") -> int:
        """Fold another session's cameras (or a ``{mac: CameraDetection}`` dict) into this one,
        de-duplicating by MAC with the strongest-RSSI rule (see :func:`_merge_camera`). Use it to
        combine two saved drives, or to reconcile a server/peer set on wifi sync. New cameras are
        copied in (no aliasing to *other*). Returns the number of cameras added or changed.
        """
        cams = other.cameras if isinstance(other, FlockSession) else dict(other)
        changed = 0
        for mac, cam in cams.items():
            prev = self.cameras.get(mac)
            if prev is None:
                self.cameras[mac] = replace(cam)
                changed += 1
            else:
                merged = _merge_camera(prev, cam)
                if merged != prev:
                    self.cameras[mac] = merged
                    changed += 1
        return changed
