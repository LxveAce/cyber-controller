"""Cache-only web API for the offline XYZ raster map tiles.

Two fixed same-origin GET handlers, projection-agnostic (no tile math beyond coordinate checks):

* :func:`tile_providers_response` -- the code-defined provider list (id / label / attribution /
  max_zoom) plus the default id. No filesystem path, location, or cache inventory is exposed, and
  never an upstream tile URL.
* :func:`tile_response` -- the cached tile's bytes with a content-sniffed image MIME. The cache
  stores PNG or JPEG under a ``.png`` name, so the type is read from the bytes. 204 when the tile
  is not cached, 404 for an unknown provider, 400 for an out-of-range coordinate, and 502 when a
  cached tile is present but unusable (corrupt magic bytes, over the byte budget, or unreadable).

Read-only and offline by contract: it reads at most one cached tile per call through a bounded
reader (never the whole file, never more than :data:`MAX_TILE_BYTES`) and NEVER fetches, writes,
scans, or falls back to the network -- online tiles remain the Qt tab's opt-in path. The route
serves cached bytes only; it never renders a map or decides a projection.
"""
from __future__ import annotations

from flask import Response, jsonify

from src.core import map_tiles

# A generous per-tile ceiling read BEFORE allocation: real 256px street tiles are a few KB to tens
# of KB, so 1 MiB is far above any legitimate tile yet bounds a corrupt or oversized cache file (the
# reader takes at most MAX_TILE_BYTES + 1 bytes, never the whole file).
MAX_TILE_BYTES = 1024 * 1024


def tile_providers_response():
    """The known providers (code-defined only) and the default id. No paths, location, cache
    inventory, or upstream URLs -- only what a future view needs to label and credit a basemap."""
    providers = [
        {"id": p.key, "label": p.label, "attribution": p.attribution, "max_zoom": p.max_zoom}
        for p in map_tiles.PROVIDERS.values()
    ]
    return jsonify({"providers": providers, "default": map_tiles.DEFAULT_PROVIDER})


def _coord_in_range(z: int, x: int, y: int, max_zoom: int) -> bool:
    if z < map_tiles.MIN_ZOOM or z > max_zoom:
        return False
    n = 1 << z
    return 0 <= x < n and 0 <= y < n


def tile_response(provider: str, z: int, x: int, y: int, cache_root=None):
    """Serve one cached tile. ``cache_root`` is an internal test seam (never request-derived),
    defaulting to the standard cache root."""
    # Provider: EXPLICIT membership -- map_tiles.get_provider() silently falls back to the default,
    # which would let an unknown name serve the default provider's cache instead of a clean 404.
    prov = map_tiles.PROVIDERS.get(provider)
    if prov is None:
        return jsonify({"error": "unknown tile provider"}), 404
    # Coordinate range BEFORE any path/cache work: z within the provider's max, x/y on the 2^z grid.
    max_zoom = min(map_tiles.MAX_ZOOM, prov.max_zoom)
    if not _coord_in_range(z, x, y, max_zoom):
        return jsonify({"error": "tile coordinate out of range"}), 400
    # Cache-only bounded read (never fetches / writes / scans).
    cache = map_tiles.TileCache(provider, root=cache_root)
    status, data = cache.read_bounded(x, y, z, MAX_TILE_BYTES)
    if status == "missing":
        return Response(status=204)                 # not cached -> the view leaves the square blank
    if status != "ok":                              # too_large / unreadable
        return jsonify({"error": "cached tile is not usable"}), 502
    mime = map_tiles.image_mime(data)
    if mime is None:                                # within budget but not a real PNG/JPEG
        return jsonify({"error": "cached tile is not a usable image"}), 502
    return Response(data, mimetype=mime)
