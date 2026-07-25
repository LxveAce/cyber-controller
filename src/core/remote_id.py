"""Drone Remote-ID (ASTM F3411) detection decoder — RX-ONLY: decode broadcasts, never transmit.

Decodes the detection output of the ``drone-mesh-mapper`` firmware
(colonelpanichacks/drone-mesh-mapper), which Cyber Controller flashes as the ``drone_mesh_mapper``
profile. That firmware passively receives WiFi Remote-ID (ASTM F3411) broadcasts from nearby drones,
decodes them on-device, and re-emits each detection over two paths:

* **USB Serial** — a compact JSON object per detection. This is the authoritative, complete format
  (drone MAC, RSSI, drone lat/long/altitude, pilot lat/long, ASTM Basic ID). Decoded by
  :func:`parse_detection_json`.
* **Meshtastic relay (Serial1)** — two human-readable lines per detection
  (``Drone: <mac> RSSI:<n> <maps-url>`` and ``Pilot: <maps-url>``), a degraded subset meant for
  at-a-glance mesh monitoring. Decoded by :func:`parse_mesh_line`.

All functions are pure over text — no Qt, no I/O, no device access, no transmit — so they unit-test
headless against golden vectors taken from the firmware's own ``snprintf`` format strings
(``remoteid-mesh/src/main.cpp``). We decode a drone's own Remote-ID broadcast (as relayed by the
detector firmware); we never author or transmit a Remote-ID frame. Grounded in firmware source,
unverified against live silicon.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# The firmware emits 0.0 for an unknown coordinate and guards the maps-URL append on both the
# latitude AND longitude being non-zero (main.cpp: ``lat_d != 0.0 && long_d != 0.0``). We mirror
# that exact convention in the ``has_*_location`` properties rather than silently null-coercing.
_UNSET = 0.0

# "Drone: aa:bb:cc:dd:ee:ff RSSI:-42[ https://maps.google.com/?q=37.123456,-122.123456]"
_RE_MESH_DRONE = re.compile(
    r"^Drone:\s*([0-9a-fA-F:]{17})\s+RSSI:(-?\d+)(?:\s+(\S+))?\s*$"
)
# "Pilot: https://maps.google.com/?q=37.111111,-122.222222"
_RE_MESH_PILOT = re.compile(r"^Pilot:\s*(\S+)\s*$")
# The "?q=<lat>,<lon>" payload of a Google Maps URL the firmware builds for a coordinate.
_RE_MAPS_Q = re.compile(r"[?&]q=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class RemoteIdDetection:
    """One decoded drone Remote-ID detection (received via the detector firmware, never sent).

    Fields carry the firmware's emitted values verbatim — a coordinate is ``0.0`` when the drone did
    not broadcast it, matching the firmware's own convention; use :attr:`has_drone_location` /
    :attr:`has_pilot_location` for the "is this a real fix" question.
    """

    mac: str            # detector-seen drone MAC, lowercase colon hex
    basic_id: str       # ASTM F3411 Basic ID (UAS serial / registration id), "" if not broadcast
    rssi: int           # detector RSSI at reception, signed dBm
    drone_lat: float    # drone latitude, decimal degrees (0.0 = not broadcast)
    drone_long: float   # drone longitude, decimal degrees (0.0 = not broadcast)
    drone_altitude: int  # drone altitude MSL, metres
    pilot_lat: float    # operator/pilot latitude, decimal degrees (0.0 = not broadcast)
    pilot_long: float   # operator/pilot longitude, decimal degrees (0.0 = not broadcast)

    @property
    def has_drone_location(self) -> bool:
        """True when the drone broadcast a usable position (both coordinates non-zero)."""
        return self.drone_lat != _UNSET and self.drone_long != _UNSET

    @property
    def has_pilot_location(self) -> bool:
        """True when the operator/pilot position was broadcast (both coordinates non-zero)."""
        return self.pilot_lat != _UNSET and self.pilot_long != _UNSET

    @property
    def label(self) -> str:
        """Best display id: the ASTM Basic ID if present, else the drone MAC."""
        return self.basic_id or self.mac


def parse_detection_json(line: str) -> "RemoteIdDetection | None":
    """Decode one USB-Serial JSON detection line from the drone-mesh-mapper firmware.

    The firmware emits, per detection::

        {"mac":"..", "rssi":N, "drone_lat":F, "drone_long":F, "drone_altitude":N,
         "pilot_lat":F, "pilot_long":F, "basic_id":".."}

    and a ``{"heartbeat":".."}`` keepalive between detections. Returns ``None`` for the heartbeat,
    for non-JSON / malformed lines, and for any object without a ``mac`` — so a keepalive or a
    stray line is never turned into a fabricated detection. Missing optional fields fall back to the
    firmware's own zero/empty defaults (mirroring the reference host's ``.get`` tolerance).
    RX/decode only.
    """
    s = line.strip()
    if not s.startswith("{"):
        return None
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or "mac" not in obj:
        return None
    try:
        return RemoteIdDetection(
            mac=str(obj["mac"]).lower(),
            basic_id=str(obj.get("basic_id", "")),
            rssi=int(obj.get("rssi", 0)),
            drone_lat=float(obj.get("drone_lat", 0.0)),
            drone_long=float(obj.get("drone_long", 0.0)),
            drone_altitude=int(obj.get("drone_altitude", 0)),
            pilot_lat=float(obj.get("pilot_lat", 0.0)),
            pilot_long=float(obj.get("pilot_long", 0.0)),
        )
    except (ValueError, TypeError):
        return None


def _coords_from_maps_url(url: str) -> "tuple[float, float] | None":
    """Pull ``(lat, lon)`` out of a ``...?q=<lat>,<lon>`` Google Maps URL, or ``None``."""
    m = _RE_MAPS_Q.search(url)
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None


def parse_mesh_line(line: str) -> "dict | None":
    """Decode one Meshtastic-relay line from the drone-mesh-mapper firmware.

    Recognises the two lines the firmware sends over ``Serial1``:

    * ``Drone: <mac> RSSI:<n>[ <maps-url>]`` → ``{"kind": "drone", "mac", "rssi",
      ["drone_lat", "drone_long"]}`` (coordinates only when the URL was appended).
    * ``Pilot: <maps-url>`` → ``{"kind": "pilot", "pilot_lat", "pilot_long"}``.

    This is a degraded subset of the JSON format (no basic_id, no altitude, pilot on a separate
    line) meant for at-a-glance monitoring; correlating a Pilot line back to its Drone is the
    caller's job. Returns ``None`` for anything else. RX/decode only.
    """
    s = line.strip()
    md = _RE_MESH_DRONE.match(s)
    if md:
        out: dict = {"kind": "drone", "mac": md.group(1).lower(), "rssi": int(md.group(2))}
        if md.group(3):
            coords = _coords_from_maps_url(md.group(3))
            if coords:
                out["drone_lat"], out["drone_long"] = coords
        return out
    mp = _RE_MESH_PILOT.match(s)
    if mp:
        coords = _coords_from_maps_url(mp.group(1))
        if coords:
            return {"kind": "pilot", "pilot_lat": coords[0], "pilot_long": coords[1]}
        return None
    return None
