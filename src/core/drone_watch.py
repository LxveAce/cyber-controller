"""Drone watch model — a bounded, TTL-aware rollup of drone Remote-ID sightings.

Folds ``drone_found`` events (from :class:`~src.protocols.drone_mesh.DroneMeshProtocol`, which
decodes the drone-mesh-mapper detector via :mod:`src.core.remote_id`) into deduplicated rows for
Tracker & Drone Watch view. A direct structural mirror of
:class:`~src.core.ble_analyzer.BleAnalyzerModel`: pure (no Qt, no I/O), bounded (the stalest row is
evicted at the cap), and TTL-aware (a sighting fades as it ages). Keyed by the drone's ASTM Basic ID
when broadcast, else its detector-seen MAC, so the same drone re-observed (a USB JSON detection plus
the mesh relay's ``Pilot:`` follow-up) upserts one row.

RX / awareness only — this notices drones, it never touches them.
"""
from __future__ import annotations

from dataclasses import dataclass

_MAX_DRONES = 512        # stalest sighting is evicted when a new drone arrives at the cap
_DEFAULT_TTL = 60.0      # seconds since last_seen after which a sighting is considered stale


@dataclass
class DroneSighting:
    """One decoded drone sighting, deduplicated + aged. A coord is ``0.0`` when not broadcast."""

    key: str
    mac: str = ""
    basic_id: str = ""
    rssi: int = 0
    drone_lat: float = 0.0
    drone_long: float = 0.0
    drone_altitude: int = 0
    pilot_lat: float = 0.0
    pilot_long: float = 0.0
    first_seen: float = 0.0
    last_seen: float = 0.0
    times_seen: int = 0

    @property
    def has_drone_location(self) -> bool:
        return self.drone_lat != 0.0 and self.drone_long != 0.0

    @property
    def has_pilot_location(self) -> bool:
        return self.pilot_lat != 0.0 and self.pilot_long != 0.0

    @property
    def label(self) -> str:
        return self.basic_id or self.mac

    def age(self, now: float) -> float:
        return max(0.0, now - self.last_seen)

    def is_fresh(self, now: float, ttl: float = _DEFAULT_TTL) -> bool:
        return self.age(now) <= ttl

    def freshness(self, now: float, ttl: float = _DEFAULT_TTL) -> float:
        """1.0 just-seen -> 0.0 at/after ttl — a stale row fades by this factor in the view."""
        if ttl <= 0:
            return 1.0
        return max(0.0, min(1.0, 1.0 - self.age(now) / ttl))


def _num(value: object, cast, default):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


class DroneWatchModel:
    """Bounded, TTL-aware rollup of drone Remote-ID sightings (mirrors ``BleAnalyzerModel``)."""

    def __init__(self, max_drones: int = _MAX_DRONES) -> None:
        self._drones: dict[str, DroneSighting] = {}
        self._max_drones = max(1, int(max_drones))

    def __len__(self) -> int:
        return len(self._drones)

    @property
    def count(self) -> int:
        return len(self._drones)

    def observe(self, data: object, now: float) -> "DroneSighting | None":
        """Fold one ``drone_found`` payload in. Returns the new/updated sighting, or None
        when the event carries no usable key (no basic_id AND no mac) — never manufactures a row."""
        if not isinstance(data, dict):
            return None
        basic_id = str(data.get("basic_id", "") or "").strip()
        mac = str(data.get("mac", "") or "").strip().lower()
        key = basic_id or mac
        if not key:
            return None
        d = self._drones.get(key)
        if d is None:
            if len(self._drones) >= self._max_drones:
                self._evict_stalest()
            d = DroneSighting(key=key, mac=mac, basic_id=basic_id, first_seen=now)
            self._drones[key] = d
        # Latest-wins on a non-empty/non-zero value (mirrors BleAnalyzerModel + CaptureRecord):
        # never clobber a known value with a sentinel (``0``/``""``), so the mesh Pilot follow-up
        # merges the operator fix onto the drone's row without wiping the drone coords.
        if mac:
            d.mac = mac
        if basic_id:
            d.basic_id = basic_id
        rssi = _num(data.get("rssi"), int, 0)
        if rssi:
            d.rssi = rssi
        for fld in ("drone_lat", "drone_long", "pilot_lat", "pilot_long"):
            val = _num(data.get(fld), float, 0.0)
            if val != 0.0:
                setattr(d, fld, val)
        alt = _num(data.get("drone_altitude"), int, 0)
        if alt:
            d.drone_altitude = alt
        d.last_seen = now
        d.times_seen += 1
        return d

    def _evict_stalest(self) -> None:
        """Drop the least-recently-seen sighting — bounds memory when many drones pass through."""
        if not self._drones:
            return
        stalest = min(self._drones.values(), key=lambda d: d.last_seen)
        self._drones.pop(stalest.key, None)

    def get(self, key: str) -> "DroneSighting | None":
        return self._drones.get(key.strip()) if isinstance(key, str) else None

    def drones(self, now: float | None = None, ttl: float = _DEFAULT_TTL,
               fresh_only: bool = False) -> "list[DroneSighting]":
        """Snapshot of sightings, most-recently-seen first. fresh_only drops rows older than ttl."""
        rows = list(self._drones.values())
        if fresh_only and now is not None:
            rows = [d for d in rows if d.is_fresh(now, ttl)]
        rows.sort(key=lambda d: d.last_seen, reverse=True)
        return rows

    def summary(self, now: float, ttl: float = _DEFAULT_TTL) -> dict:
        """Header counts: total tracked, fresh, and how many carry a drone / pilot position fix."""
        rows = list(self._drones.values())
        return {
            "total": len(rows),
            "fresh": sum(1 for d in rows if d.is_fresh(now, ttl)),
            "with_drone_location": sum(1 for d in rows if d.has_drone_location),
            "with_pilot_location": sum(1 for d in rows if d.has_pilot_location),
        }
