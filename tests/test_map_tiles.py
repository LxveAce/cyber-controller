"""XYZ tile math + offline-first cache (src/core/map_tiles.py). Pure/no-Qt; the fetch path is monkeypatched
so no test ever touches the network.

The load-bearing test is `test_tile_aligns_with_world_px_projection`: a tile must land on the SAME shared
web-mercator plane the camera dots are projected into, or the street basemap would sit skewed under them.
"""
from __future__ import annotations

import math

from src.core import map_tiles as mt

# world_px / _WORLD_PX live ABOVE flock_heatmap_tab's PyQt try-block, so they import without Qt.
from src.ui.qt.flock_heatmap_tab import _WORLD_PX, world_px

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32          # a byte string that passes the image magic check
_JPEG = b"\xff\xd8\xff" + b"\x00" * 32


def test_world_px_constant_cannot_drift_from_the_map():
    # The tile plane and the camera projection MUST share the same world size, or tiles sit skewed under dots.
    assert mt.WORLD_PX == _WORLD_PX


def test_tile_indices_stay_on_grid():
    for z in (0, 1, 5, 12, 19):
        n = 1 << z
        for lat, lon in [(0, 0), (51.5, -0.12), (-33.86, 151.2), (85.5, 200), (-90, -400)]:
            x, y = mt.tile_xy(lat, lon, z)
            assert 0 <= x < n and 0 <= y < n, (lat, lon, z, x, y)


def test_tile_world_rect_size_and_paving():
    # A tile's edge is WORLD_PX/2^z, and the 2^z x 2^z grid paves the whole world exactly.
    for z in (0, 1, 8):
        n = 1 << z
        size = mt.WORLD_PX / n
        rx, ry, sz = mt.tile_world_rect(0, 0, z)
        assert (rx, ry) == (0.0, 0.0) and math.isclose(sz, size)
        # last tile's far corner is the world edge
        rx2, ry2, _ = mt.tile_world_rect(n - 1, n - 1, z)
        assert math.isclose(rx2 + size, mt.WORLD_PX) and math.isclose(ry2 + size, mt.WORLD_PX)


def test_tile_aligns_with_world_px_projection():
    # THE alignment invariant: the tile containing a point must have a world-rect that contains that point's
    # world_px projection — so the street tiles line up with the camera dots at every zoom.
    for lat, lon in [(51.5074, -0.1278), (40.7128, -74.0060), (-33.8688, 151.2093), (35.68, 139.69)]:
        wx, wy = world_px(lat, lon)
        for z in (2, 10, 16, 19):
            x, y = mt.tile_xy(lat, lon, z)
            rx, ry, sz = mt.tile_world_rect(x, y, z)
            assert rx <= wx <= rx + sz, (lat, lon, z, "x")
            assert ry <= wy <= ry + sz, (lat, lon, z, "y")


def test_tiles_in_world_rect_covers_and_clamps():
    # A small box around a point at z=14 returns a compact set, all on-grid, including the point's own tile.
    lat, lon, z = 51.5074, -0.1278, 14
    wx, wy = world_px(lat, lon)
    size = mt.WORLD_PX / (1 << z)
    tiles = mt.tiles_in_world_rect(wx - size, wy - size, wx + size, wy + size, z)
    tx, ty = mt.tile_xy(lat, lon, z)
    assert (tx, ty, z) in tiles
    assert len(tiles) <= 9                       # a ~2-tile-wide box is at most 3x3
    n = 1 << z
    assert all(0 <= tx < n and 0 <= ty < n and tz == z for tx, ty, tz in tiles)
    # A rect covering the whole world at z=1 returns all four tiles.
    assert len(mt.tiles_in_world_rect(0, 0, mt.WORLD_PX, mt.WORLD_PX, 1)) == 4


def test_zoom_for_world_per_px_is_monotonic_and_clamped():
    # More world units per screen pixel (zoomed out) => a lower tile zoom.
    z_out = mt.zoom_for_world_per_px(10_000.0)
    z_mid = mt.zoom_for_world_per_px(100.0)
    z_in = mt.zoom_for_world_per_px(1.0)
    assert z_out < z_mid < z_in
    assert mt.zoom_for_world_per_px(0.0) == mt.MAX_ZOOM         # degenerate -> deepest
    assert mt.MIN_ZOOM <= mt.zoom_for_world_per_px(1e12) <= mt.MAX_ZOOM


def test_tiles_for_viewport_matches_scale_and_caps():
    lat, lon = 51.5074, -0.1278
    wx, wy = world_px(lat, lon)
    # A ~1000 world-unit view centered on a point at a street scale returns a small tile set at a deep zoom.
    z, tiles = mt.tiles_for_viewport(wx - 500, wy - 500, wx + 500, wy + 500, world_per_px=4.0)
    assert z >= 14 and 0 < len(tiles) <= 80
    assert all(tz == z for _, _, tz in tiles)
    # A cap below the covering-tile count returns [] (the safety bound), not a flood.
    _, capped = mt.tiles_for_viewport(wx - 500, wy - 500, wx + 500, wy + 500, world_per_px=4.0, cap=1)
    assert capped == []


def test_tiles_for_viewport_large_view_needs_a_bigger_cap():
    # Capstone fix: a maximized 4K/ultrawide view is a scale-matched ~130 tiles — the old default cap=80
    # dropped the whole basemap to []. A viewport-sized cap returns the full set.
    lat, lon = 51.5074, -0.1278
    wx, wy = world_px(lat, lon)
    half_w, half_h = 3840 * 4 / 2, 2160 * 4 / 2          # ~4K view at ~4 world-units/screen-px
    _, dropped = mt.tiles_for_viewport(wx - half_w, wy - half_h, wx + half_w, wy + half_h, 4.0, cap=80)
    assert dropped == []                                 # the old default silently blanks the basemap
    _, full = mt.tiles_for_viewport(wx - half_w, wy - half_h, wx + half_w, wy + half_h, 4.0, cap=400)
    assert len(full) > 80                                # a viewport-derived cap returns them all


def test_get_provider_fallback():
    assert mt.get_provider("osm").key == "osm"
    assert mt.get_provider("nonsense").key == mt.DEFAULT_PROVIDER
    assert mt.get_provider("").key == mt.DEFAULT_PROVIDER
    assert "OpenStreetMap contributors" in mt.get_provider("osm").attribution


def test_cache_store_get_roundtrip_and_path(tmp_path):
    c = mt.TileCache(provider="osm", root=tmp_path)
    assert c.get(1, 2, 3) is None and not c.has(1, 2, 3)
    assert c.store(1, 2, 3, _PNG) is True
    assert c.has(1, 2, 3) and c.get(1, 2, 3) == _PNG
    # Path layout is <root>/<provider>/{z}/{x}/{y}.png
    assert c.path_for(1, 2, 3) == tmp_path / "osm" / "3" / "1" / "2.png"


def test_cache_rejects_non_image_bytes(tmp_path):
    c = mt.TileCache(root=tmp_path)
    assert c.store(0, 0, 0, b"<html>rate limited</html>") is False
    assert not c.has(0, 0, 0)
    assert c.store(0, 0, 0, _JPEG) is True         # a JPEG tile is accepted too


def test_get_or_fetch_is_cache_first_and_offline_safe(tmp_path, monkeypatch):
    c = mt.TileCache(root=tmp_path)
    calls = []
    monkeypatch.setattr(c, "fetch", lambda x, y, z: calls.append((x, y, z)) or None)
    # Offline / opt-out: a missing tile returns None and NEVER hits the network.
    assert c.get_or_fetch(5, 6, 7, allow_network=False) is None
    assert calls == []
    # A cached tile is returned without a fetch even when network is allowed.
    c.store(5, 6, 7, _PNG)
    assert c.get_or_fetch(5, 6, 7, allow_network=True) == _PNG
    assert calls == []


def test_fetch_downloads_and_caches(tmp_path, monkeypatch):
    c = mt.TileCache(root=tmp_path)

    class _Resp:
        status = 200

        def read(self):
            return _PNG

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["ua"] = req.get_header("User-agent")
        return _Resp()

    monkeypatch.setattr(mt.urllib.request, "urlopen", fake_urlopen)
    data = c.fetch(10, 20, 15)
    assert data == _PNG
    assert c.has(10, 20, 15)                        # fetched tile is cached for offline reuse
    assert seen["url"] == "https://tile.openstreetmap.org/15/10/20.png"
    assert "CyberController" in (seen["ua"] or "")  # descriptive UA per tile-server policy


def test_fetch_rejects_non_200_and_non_image(tmp_path, monkeypatch):
    c = mt.TileCache(root=tmp_path)

    class _Resp:
        def __init__(self, status, body):
            self.status = status
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mt.urllib.request, "urlopen", lambda req, timeout=None: _Resp(404, _PNG))
    assert c.fetch(1, 1, 1) is None and not c.has(1, 1, 1)
    monkeypatch.setattr(mt.urllib.request, "urlopen", lambda req, timeout=None: _Resp(200, b"not-an-image"))
    assert c.fetch(2, 2, 2) is None and not c.has(2, 2, 2)


def test_fetch_is_offline_safe(tmp_path, monkeypatch):
    c = mt.TileCache(root=tmp_path)

    def boom(req, timeout=None):
        raise mt.urllib.error.URLError("offline")

    monkeypatch.setattr(mt.urllib.request, "urlopen", boom)
    assert c.fetch(3, 3, 3) is None                 # a network error is swallowed, never raised
