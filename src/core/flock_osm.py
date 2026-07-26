"""Import crowdsourced ALPR camera locations from OpenStreetMap via the Overpass API — the DeFlock
awareness-map layer, DISTINCT from the RF sniffer log.

Pure + offline: this module only PARSES an Overpass JSON response into awareness-only
:class:`~src.core.flock.CameraDetection` records; a host fetches the response separately (gated).
The records carry no RF data (rssi/channel/frequency = 0 — a map entry is not a live detection) and
are tagged ``detection_method="osm-overpass"`` so DB-imported cameras never pose as RF hits. Like
Flock-You detection, this is awareness-only — it maps where cameras are, never an attack path.
OSM data is ODbL-licensed: attribution is required wherever it is shown or exported.

Grounded on a real Overpass response (verified 2026-07-26): the top-level object carries an
``elements`` array of ``{"type": "node", "id": int, "lat": float, "lon": float, "tags": {...}}``; a
DeFlock ALPR node carries ``man_made=surveillance`` + ``surveillance:type=ALPR`` + ``manufacturer``
(e.g. "Flock Safety") and/or ``operator``, often ``direction``. All tag values are strings.
"""
from __future__ import annotations

from src.core.flock import CameraDetection

#: detection_method stamped on OSM/Overpass-imported cameras — keeps them apart from RF detections.
OSM_DETECTION_METHOD = "osm-overpass"


def _is_num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def cameras_from_overpass(overpass: dict) -> "list[CameraDetection]":
    """Parse an Overpass JSON response into awareness-only ALPR :class:`CameraDetection` records.

    Only ``man_made=surveillance`` nodes with real coordinates become cameras (the Overpass query
    already filters, but a broad response stays honest). The OSM node id is the stable identity
    (``osm:<id>`` — clearly NOT a real MAC), the operator (else manufacturer) tag becomes the label,
    and rssi/channel/frequency stay 0. Pure: no network, no I/O; robust to a missing/odd shape.
    """
    out: "list[CameraDetection]" = []
    elements = overpass.get("elements") if isinstance(overpass, dict) else None
    for el in elements or []:
        if not isinstance(el, dict) or el.get("type") != "node":
            continue
        lat, lon = el.get("lat"), el.get("lon")
        if not (_is_num(lat) and _is_num(lon)):
            continue  # a camera with no location isn't a map point
        tags = el.get("tags")
        tags = tags if isinstance(tags, dict) else {}
        if tags.get("man_made") != "surveillance":
            continue  # only surveillance nodes — never parse an unrelated OSM node into a "camera"
        label = tags.get("operator") or tags.get("manufacturer") or ""
        out.append(CameraDetection(
            mac=f"osm:{el.get('id')}",
            lat=float(lat),
            lon=float(lon),
            ssid=str(label),
            detection_method=OSM_DETECTION_METHOD,
            count=1,
        ))
    return out


def geojson_from_overpass(overpass: dict) -> dict:
    """Overpass JSON → a cameras GeoJSON FeatureCollection (via ``to_feature``) — the shape the
    Flock heatmap's ``load_geojson_file`` already consumes."""
    return {
        "type": "FeatureCollection",
        "features": [cam.to_feature() for cam in cameras_from_overpass(overpass)],
    }
