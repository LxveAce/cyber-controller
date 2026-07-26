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

import json
import os
import time
import urllib.parse
import urllib.request

from src.core.flock import CameraDetection

#: detection_method stamped on OSM/Overpass-imported cameras — keeps them apart from RF detections.
OSM_DETECTION_METHOD = "osm-overpass"

#: Public Overpass API endpoint (fixed host — no user-controlled host, so no SSRF surface).
OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
#: Descriptive User-Agent (Overpass asks callers to identify themselves).
OVERPASS_UA = "cyber-controller flock-osm (awareness-only ALPR map; ODbL)"
#: Attribution the UI must surface wherever OSM-sourced cameras are shown or exported.
ODBL_ATTRIBUTION = "© OpenStreetMap contributors — ODbL (via the Overpass API)"


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


# ── gated fetch (query-builder + user-initiated cached runner) ───────────────────────────────────


def build_overpass_query(bbox: "tuple[float, float, float, float]", *,
                         limit: int = 2000, timeout: int = 60) -> str:
    """Build an OverpassQL query for ALPR surveillance-camera nodes in *bbox*.

    ``bbox`` = (south, west, north, east) decimal degrees (OverpassQL bbox order). Returns an
    ``[out:json]`` node query for ``man_made=surveillance`` + ``surveillance:type=ALPR``, capped at
    ``limit`` results. Grounded on the query verified working against overpass-api.de (2026-07-26).
    """
    s, w, n, e = (float(x) for x in bbox)
    if not (-90.0 <= s <= n <= 90.0 and -180.0 <= w <= e <= 180.0):
        raise ValueError(f"invalid bbox (need S<=N in [-90,90], W<=E in [-180,180]): {bbox}")
    return (
        f"[out:json][timeout:{max(1, int(timeout))}];"
        f'node["man_made"="surveillance"]["surveillance:type"="ALPR"]'
        f"({s},{w},{n},{e});"
        f"out {max(1, int(limit))};"
    )


def _default_fetch(url: str, *, timeout: int = 90) -> str:
    """The real Overpass GET (urllib + descriptive UA). GATED: reached only via a user-initiated
    fetch when the cache is cold — never at import/startup. Fixed https host, so no SSRF surface."""
    req = urllib.request.Request(url, headers={"User-Agent": OVERPASS_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _cache_fresh(path: str, max_age_secs: int) -> bool:
    try:
        return (time.time() - os.path.getmtime(path)) < max_age_secs
    except OSError:
        return False


def fetch_alpr_geojson(bbox: "tuple[float, float, float, float]", cache_path: "str | None" = None,
                       *, fetcher=None, max_age_secs: int = 86400) -> dict:
    """USER-INITIATED import of ALPR camera locations for *bbox* → a cameras GeoJSON collection.

    GATED — call ONLY on an explicit user action, NEVER auto-run. Respects the shared free Overpass
    API: if *cache_path* holds a response younger than *max_age_secs* (default 24h) it is reused
    with NO network call (the offline cache IS the rate-limit); else ONE query is fetched + cached
    atomically. *fetcher(url)->str* overrides the network (tests inject it). Awareness-only, drives
    no device; callers surface :data:`ODBL_ATTRIBUTION` wherever the cameras are shown or exported.
    """
    if cache_path and _cache_fresh(cache_path, max_age_secs):
        with open(cache_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    url = OVERPASS_ENDPOINT + "?data=" + urllib.parse.quote(build_overpass_query(bbox))
    geojson = geojson_from_overpass(json.loads((fetcher or _default_fetch)(url)))
    if cache_path:
        tmp = cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(geojson, fh)
        os.replace(tmp, cache_path)
    return geojson
