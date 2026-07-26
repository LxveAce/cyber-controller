"""Drone Remote-ID (ASTM F3411) protocol — passive, receive-only detector parser.

Cyber Controller flashes the ``drone_mesh_mapper`` profile (colonelpanichacks/drone-mesh-mapper), a
passive WiFi Remote-ID detector that decodes nearby drones' broadcasts on-device and re-emits them
over serial. This protocol turns that serial output into structured ``drone_found`` events by
delegating to the pure, RX-only decoder :mod:`src.core.remote_id` (grounded in the firmware's own
``snprintf`` format strings). Passive / receive-only: there is NO command channel and this parser
authors no frame — it decodes a drone's own broadcast (as relayed by the detector), the same posture
as :class:`~src.protocols.flock_you.FlockYouProtocol` (the ALPR-camera detector).

The firmware emits two output paths (see :mod:`src.core.remote_id`): a complete USB-Serial JSON
detection line (primary), and a degraded Meshtastic-relay text pair (``Drone: ...`` then
 ``Pilot: ...``) for at-a-glance monitoring. Both become ``drone_found``; a ``Pilot:`` line
carries only the operator position, so it is correlated onto the most recent ``Drone:`` line's MAC.

``drone_found`` is a NEW event type that ``target_ingest`` does not handle (like the nrf24/iot
terminal-only events): a drone is NOT routed into the shared Target pool / AutoRouter — it feeds a
dedicated :class:`~src.core.drone_watch.DroneWatchModel`. RX-only; grounded in firmware source,
unverified against live silicon.
"""
from __future__ import annotations

from src.core.remote_id import RemoteIdDetection, parse_detection_json, parse_mesh_line
from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent


def _detection_data(det: RemoteIdDetection, source: str) -> dict:
    """The ``drone_found`` payload for a fully-decoded USB-Serial detection."""
    return {
        "mac": det.mac,
        "basic_id": det.basic_id,
        "rssi": det.rssi,
        "drone_lat": det.drone_lat,
        "drone_long": det.drone_long,
        "drone_altitude": det.drone_altitude,
        "pilot_lat": det.pilot_lat,
        "pilot_long": det.pilot_long,
        "has_drone_location": det.has_drone_location,
        "has_pilot_location": det.has_pilot_location,
        "label": det.label,
        "source": source,
    }


class DroneMeshProtocol(BaseProtocol):
    """Parser for the drone-mesh-mapper passive Remote-ID detector (receive-only, no CLI)."""

    driver_type = "controlmap"           # passive sensor: no text CLI (mirrors FlockYouProtocol)
    capabilities = frozenset({"wifi"})   # a WiFi Remote-ID (ASTM F3411) receiver

    def __init__(self) -> None:
        super().__init__()
        # A Meshtastic ``Pilot:`` line carries only the operator position; correlate it onto the
        # MAC of the most recent ``Drone:`` line (firmware emits Drone then Pilot for one drone).
        self._last_mesh_mac = ""

    @property
    def protocol_name(self) -> str:
        return "drone-mesh"

    def parse_line(self, line: str) -> ParsedEvent | None:
        line = (line or "").strip()
        if not line:
            return None
        # 1) Primary: the complete USB-Serial JSON detection line.
        if line.startswith("{"):
            det = parse_detection_json(line)
            if det is not None:
                return ParsedEvent("drone_found", _detection_data(det, "usb"), line)
            # A heartbeat keepalive between detections -> surface as info, not a detection.
            if '"heartbeat"' in line:
                return ParsedEvent("info", {"message": "drone detector heartbeat"}, line)
            return None
        # 2) Degraded: the Meshtastic-relay text (``Drone:`` / ``Pilot:`` lines).
        mesh = parse_mesh_line(line)
        if mesh is not None:
            return self._mesh_event(mesh, line)
        return None

    def _mesh_event(self, mesh: dict, raw: str) -> ParsedEvent | None:
        kind = mesh.get("kind")
        if kind == "drone":
            mac = str(mesh.get("mac", ""))
            self._last_mesh_mac = mac
            drone_lat = float(mesh.get("drone_lat", 0.0))
            drone_long = float(mesh.get("drone_long", 0.0))
            data = {
                "mac": mac, "basic_id": "", "rssi": int(mesh.get("rssi", 0)),
                "drone_lat": drone_lat, "drone_long": drone_long, "drone_altitude": 0,
                "pilot_lat": 0.0, "pilot_long": 0.0,
                "has_drone_location": drone_lat != 0.0 and drone_long != 0.0,
                "has_pilot_location": False, "label": mac, "source": "mesh",
            }
            return ParsedEvent("drone_found", data, raw)
        if kind == "pilot":
            if not self._last_mesh_mac:
                return None   # a Pilot line with no preceding Drone line — nothing to key it to
            pilot_lat = float(mesh.get("pilot_lat", 0.0))
            pilot_long = float(mesh.get("pilot_long", 0.0))
            data = {
                "mac": self._last_mesh_mac, "basic_id": "", "rssi": 0,
                "drone_lat": 0.0, "drone_long": 0.0, "drone_altitude": 0,
                "pilot_lat": pilot_lat, "pilot_long": pilot_long,
                "has_drone_location": False,
                "has_pilot_location": pilot_lat != 0.0 and pilot_long != 0.0,
                "label": self._last_mesh_mac, "source": "mesh",
            }
            return ParsedEvent("drone_found", data, raw)
        return None

    def get_commands(self) -> list[CommandInfo]:
        return []   # passive detector — nothing to send

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        if args:
            return f"{cmd} " + " ".join(str(v) for v in args.values())
        return cmd

    def identify(self, line: str) -> bool:
        low = (line or "").lower()
        if '"drone_lat"' in low and '"basic_id"' in low:
            return True   # the USB-Serial JSON detection line
        if '"heartbeat"' in low and "device is active" in low:
            return True   # the detector's keepalive
        return low.startswith("drone:") and "rssi:" in low   # the Meshtastic relay line
