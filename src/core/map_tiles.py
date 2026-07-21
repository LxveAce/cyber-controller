"""XYZ raster map tiles for the Flock map — pure web-mercator tile math + an offline-first disk cache.

The Flock map renders every layer in one shared web-mercator plane (``flock_heatmap_tab.world_px`` maps a
lat/lon into ``[0, WORLD_PX]``). Standard slippy-map **XYZ tiles are also web-mercator**, so a tile ``(z, x, y)``
covers a known rectangle of that same plane — which is what lets a real street basemap drop in *under* the camera
dots with no reprojection and no QtWebEngine dependency. This module is the plumbing:

- the pure tile<->world math (unit-testable, no Qt, no I/O),
- a disk cache at ``~/.cyber-controller/map-tiles/<provider>/{z}/{x}/{y}.png``,
- and an **opt-in, viewport-scoped** online fetch that respects tile-server usage policy (a descriptive
  User-Agent, cache-first, one tile per request, never a bulk/area scrape).

Offline-first by design: with no network — the app's normal state in a moving vehicle — the map renders whatever
tiles are already cached and leaves the rest blank. When the operator opts into online tiles while connected,
the tiles for the *current view* are fetched once and cached, so the same area then works offline forever.
"""
from __future__ import annotations

import math
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Earth's equatorial circumference in metres — the edge length of the full web-mercator world square in the
# Flock map's shared plane. MUST equal ``flock_heatmap_tab._WORLD_PX`` (a test asserts they can't drift), so a
# tile placed via :func:`tile_world_rect` lands exactly on the cameras projected via ``world_px``.
WORLD_PX: float = 40_075_016.0

TILE_SIZE: int = 256          # standard slippy-tile edge in pixels
MIN_ZOOM: int = 0
MAX_ZOOM: int = 19            # OSM's deepest standard raster zoom

# Latitude beyond which web-mercator is undefined (the poles map to ±infinity). Clamp to it.
_MERC_LAT_LIMIT: float = 85.05112878


# ── XYZ tile providers ──────────────────────────────────────────────────────
# Each provider = an HTTPS XYZ URL template + the attribution its data license requires (OSM's ODbL mandates
# visible credit). Kept small and code-controlled: the operator picks a name, never a raw URL, so a hostile
# string can't redirect the fetch. Default is the OSM standard layer.
class Provider:
    """An XYZ tile source: a ``{z}/{x}/{y}`` URL template plus its required on-map attribution."""

    def __init__(self, key: str, label: str, url_template: str, attribution: str, max_zoom: int = MAX_ZOOM):
        self.key = key
        self.label = label
        self.url_template = url_template
        self.attribution = attribution
        self.max_zoom = max_zoom

    def url(self, x: int, y: int, z: int) -> str:
        return (self.url_template
                .replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y)))

    @property
    def host(self) -> str:
        from urllib.parse import urlparse
        return urlparse(self.url_template).hostname or ""


PROVIDERS: Dict[str, Provider] = {
    "osm": Provider(
        "osm", "OpenStreetMap",
        "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "© OpenStreetMap contributors"),
    "osm-de": Provider(
        "osm-de", "OpenStreetMap (DE)",
        "https://tile.openstreetmap.de/{z}/{x}/{y}.png",
        "© OpenStreetMap contributors"),
}
DEFAULT_PROVIDER = "osm"


def get_provider(key: str) -> Provider:
    """Resolve a provider by key, falling back to the default for an unknown key."""
    return PROVIDERS.get((key or "").strip().lower(), PROVIDERS[DEFAULT_PROVIDER])


# ── pure tile <-> world math (no Qt, no I/O) ────────────────────────────────
def clamp_zoom(z: int, max_zoom: int = MAX_ZOOM) -> int:
    """Clamp a zoom level into ``[MIN_ZOOM, max_zoom]``."""
    return max(MIN_ZOOM, min(int(z), max_zoom))


def lonlat_to_tile_frac(lat: float, lon: float, z: int) -> Tuple[float, float]:
    """Fractional tile coordinates of (lat, lon) at zoom *z* (the standard slippy formula). The integer
    parts are the tile indices; the fractions locate the point inside the tile. Latitude is clamped to the
    web-mercator limit so a bad fix can't produce a non-finite tile."""
    z = clamp_zoom(z)
    n = float(1 << z)
    lat = max(min(lat, _MERC_LAT_LIMIT), -_MERC_LAT_LIMIT)
    lat_rad = math.radians(lat)
    xf = (lon + 180.0) / 360.0 * n
    yf = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return xf, yf


def tile_xy(lat: float, lon: float, z: int) -> Tuple[int, int]:
    """Integer XYZ tile indices containing (lat, lon) at zoom *z*, clamped to the valid ``[0, 2^z-1]`` grid."""
    z = clamp_zoom(z)
    n = 1 << z
    xf, yf = lonlat_to_tile_frac(lat, lon, z)
    x = min(n - 1, max(0, int(math.floor(xf))))
    y = min(n - 1, max(0, int(math.floor(yf))))
    return x, y


def tile_world_rect(x: int, y: int, z: int) -> Tuple[float, float, float]:
    """The ``(world_x, world_y, size)`` rectangle a tile occupies in the shared ``world_px`` plane. Because
    both this and ``flock_heatmap_tab.world_px`` use the same normalized web-mercator × :data:`WORLD_PX`, the
    tile aligns exactly with the projected cameras. *size* is the tile's edge in world units."""
    z = clamp_zoom(z)
    size = WORLD_PX / float(1 << z)
    return x * size, y * size, size


# A hard backstop on how many tiles :func:`tiles_in_world_rect` will ever materialize, so a deep-zoom query
# over a huge world box (e.g. the whole world at z17 = ~17 billion tiles) returns [] instead of OOM-ing. No
# real viewport needs more than a few dozen tiles; 4096 (a 64×64 grid) is far above any sane request.
_MAX_TILES_HARD = 4096


def _tile_range(wx0: float, wy0: float, wx1: float, wy1: float, z: int) -> Tuple[int, int, int, int]:
    """Inclusive ``(x0, x1, y0, y1)`` tile-index range covering the (normalized, grid-clamped) world box."""
    n = 1 << clamp_zoom(z)
    size = WORLD_PX / float(n)
    lo_x, hi_x = sorted((wx0, wx1))
    lo_y, hi_y = sorted((wy0, wy1))
    x0 = max(0, min(n - 1, int(math.floor(lo_x / size))))
    x1 = max(0, min(n - 1, int(math.floor(hi_x / size))))
    y0 = max(0, min(n - 1, int(math.floor(lo_y / size))))
    y1 = max(0, min(n - 1, int(math.floor(hi_y / size))))
    return x0, x1, y0, y1


def tiles_in_world_rect(wx0: float, wy0: float, wx1: float, wy1: float, z: int) -> List[Tuple[int, int, int]]:
    """Every ``(x, y, z)`` tile whose world-rect intersects the world-plane box [wx0,wy0]-[wx1,wy1]. This is
    the viewport query: the map loads exactly the tiles the visible area needs. Indices are clamped to the
    ``2^z`` grid, and the box is normalized so a swapped/negative rect still works. Returns ``[]`` if the box
    would span more than :data:`_MAX_TILES_HARD` tiles, so a pathological query can never exhaust memory."""
    z = clamp_zoom(z)
    x0, x1, y0, y1 = _tile_range(wx0, wy0, wx1, wy1, z)
    if (x1 - x0 + 1) * (y1 - y0 + 1) > _MAX_TILES_HARD:
        return []
    return [(x, y, z) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def zoom_for_world_per_px(world_per_px: float, max_zoom: int = MAX_ZOOM) -> int:
    """Pick the tile zoom whose tiles render closest to their native 256 px on screen, given how many world
    units cover one screen pixel at the current view scale. A tile spans ``WORLD_PX/2^z`` world units over
    :data:`TILE_SIZE` px, so we want ``WORLD_PX/2^z ≈ TILE_SIZE·world_per_px`` → ``z ≈ log2(WORLD_PX/(TILE_SIZE·
    world_per_px))``. Degenerate/zero input pins to the deepest zoom."""
    if world_per_px <= 0 or not math.isfinite(world_per_px):
        return clamp_zoom(max_zoom, max_zoom)
    z = math.log2(WORLD_PX / (TILE_SIZE * world_per_px))
    return clamp_zoom(int(round(z)), max_zoom)


def tiles_for_viewport(wx0: float, wy0: float, wx1: float, wy1: float, world_per_px: float,
                       max_zoom: int = MAX_ZOOM, cap: int = 80) -> Tuple[int, List[Tuple[int, int, int]]]:
    """Given the visible world-plane rectangle and the view's world-units-per-screen-pixel, return
    ``(zoom, tiles)`` — the tile zoom matched to the scale and the tiles covering the view. The list is
    **capped**: if a (pathological) view would need more than *cap* tiles it returns an empty list rather than
    firing off a flood of requests. At a scale-matched zoom the count is naturally small (viewport_px/256+1)²,
    so the cap only ever trips on a mismatch. Pure — the tab's loader is a thin wrapper over this."""
    z = zoom_for_world_per_px(world_per_px, max_zoom)
    tiles = tiles_in_world_rect(wx0, wy0, wx1, wy1, z)
    if len(tiles) > cap:
        return z, []
    return z, tiles


# ── offline-first disk cache (+ opt-in online fetch) ────────────────────────
def default_cache_root() -> Path:
    """Where cached tiles live: ``$CC_MAP_TILE_DIR`` or ``~/.cyber-controller/map-tiles``. Mirrors the
    wordlist/tools/macros dirs so all persistent CC state sits under one home folder."""
    env = os.environ.get("CC_MAP_TILE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".cyber-controller" / "map-tiles"


def _looks_like_png_or_jpeg(data: bytes) -> bool:
    """A cheap magic-byte check so a fetch that returned an HTML error page (rate-limit / captcha / 404 body
    with a 200) is never cached or drawn as if it were a tile."""
    if len(data) < 4:
        return False
    return data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff"


class TileCache:
    """Offline-first tile store: reads from ``<root>/<provider>/{z}/{x}/{y}.png`` and, only when explicitly
    asked, fetches a *single* missing tile over HTTPS and caches it. Never raises to the caller — a missing
    tile (offline, or a failed fetch) simply returns ``None`` and the map leaves that square blank.

    Network use is deliberately conservative to respect tile-server policy: opt-in per call, one tile per
    request, cache-first (a cached tile is never re-fetched), and a descriptive User-Agent. Bulk/area
    downloading is never done here — the map only ever asks for the tiles of the current viewport.
    """

    #: A descriptive UA is required by OSM's tile policy; identify the app and give a contact URL.
    USER_AGENT = "CyberController-FlockMap/1.0 (+https://cybercontroller.org; authorized-research)"

    def __init__(self, provider: str = DEFAULT_PROVIDER, root: Optional[Path] = None,
                 timeout: float = 6.0) -> None:
        self.provider = get_provider(provider)
        self.root = Path(root) if root is not None else default_cache_root()
        self.timeout = timeout

    def path_for(self, x: int, y: int, z: int) -> Path:
        return self.root / self.provider.key / str(z) / str(x) / f"{y}.png"

    def has(self, x: int, y: int, z: int) -> bool:
        p = self.path_for(x, y, z)
        try:
            return p.is_file() and p.stat().st_size > 0
        except OSError:
            return False

    def get(self, x: int, y: int, z: int) -> Optional[bytes]:
        """Cached tile bytes, or ``None`` if not cached / unreadable."""
        p = self.path_for(x, y, z)
        try:
            if p.is_file():
                data = p.read_bytes()
                return data or None
        except OSError:
            return None
        return None

    def store(self, x: int, y: int, z: int, data: bytes) -> bool:
        """Cache *data* for a tile, atomically (temp file + replace so a crash mid-write can't leave a torn
        tile). Returns True on success. Rejects non-image bytes so an error page is never cached."""
        if not _looks_like_png_or_jpeg(data):
            return False
        p = self.path_for(x, y, z)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(f".png.tmp-{os.getpid()}")
            tmp.write_bytes(data)
            os.replace(tmp, p)
            return True
        except OSError:
            return False

    def fetch(self, x: int, y: int, z: int) -> Optional[bytes]:
        """Download ONE tile over HTTPS from the provider and cache it. Returns the bytes on success, ``None``
        on any failure (offline, timeout, non-200, non-image, HTTP error). Guarded — never raises."""
        url = self.provider.url(x, y, z)
        if not url.lower().startswith("https://"):   # never fetch over plaintext
            return None
        req = urllib.request.Request(url, headers={"User-Agent": self.USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 — https-only, code-controlled host
                if getattr(resp, "status", 200) != 200:
                    return None
                data = resp.read()
        except (urllib.error.URLError, OSError, ValueError):
            return None
        if not _looks_like_png_or_jpeg(data):
            return None
        self.store(x, y, z, data)
        return data

    def get_or_fetch(self, x: int, y: int, z: int, allow_network: bool = False) -> Optional[bytes]:
        """Cache-first tile read. Returns cached bytes immediately; if missing AND *allow_network* is set,
        fetches once and caches. Offline or opt-out → ``None`` (blank square). This is the method the map's
        tile loader calls per visible tile."""
        cached = self.get(x, y, z)
        if cached is not None:
            return cached
        if allow_network:
            return self.fetch(x, y, z)
        return None
