"""Route tests for the cached raster tile API (src/ui/web/map_cache_api.py).

A minimal in-process Flask app registers ONLY the two handlers over an owned TEMP tile cache; it
does NOT construct the real CC runtime, managers, sockets, or owner state. The tests cover the fixed
contract -- provider list, cached PNG/JPEG (MIME by content, not the .png suffix), missing (204),
unknown provider (404), out-of-range coordinate (400), and unusable cached content (502) -- and
assert no outbound network call and no cache write ever happens. The @requires_auth wrapper and the
app lifecycle are outside this inert test (they are exercised by the app-level suites).
"""
from __future__ import annotations

from pathlib import Path

from flask import Flask

from src.core import map_tiles
from src.ui.web import map_cache_api

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64          # valid PNG magic
_JPEG = b"\xff\xd8\xff" + b"\x00" * 64              # valid JPEG magic


def _client(cache_root):
    app = Flask(__name__)
    app.add_url_rule("/api/maps/tile-providers", "tp",
                     lambda: map_cache_api.tile_providers_response())
    app.add_url_rule("/api/map-tiles/<provider>/<int:z>/<int:x>/<int:y>.png", "tile",
                     lambda provider, z, x, y: map_cache_api.tile_response(
                         provider, z, x, y, cache_root=cache_root))
    return app.test_client()


def _store(cache_root, provider, z, x, y, data):
    p = Path(cache_root) / provider / str(z) / str(x) / f"{y}.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _files(root):
    return sorted(p.relative_to(root).as_posix() for p in Path(root).rglob("*") if p.is_file())


def test_tile_providers_lists_code_defined_providers_without_urls(tmp_path):
    resp = _client(tmp_path).get("/api/maps/tile-providers")
    assert resp.status_code == 200
    doc = resp.get_json()
    assert doc["default"] == map_tiles.DEFAULT_PROVIDER
    ids = {p["id"] for p in doc["providers"]}
    assert ids == set(map_tiles.PROVIDERS)
    for p in doc["providers"]:
        # only these fields: no url/url_template/path/location
        assert set(p) == {"id", "label", "attribution", "max_zoom"}
    # no upstream URL/host is ever exposed as a browser fetch path
    blob = resp.get_data(as_text=True)
    assert "http" not in blob and "cartocdn" not in blob and "openstreetmap.org" not in blob


def test_serves_a_cached_png(tmp_path):
    _store(tmp_path, "carto-dark", 3, 4, 5, _PNG)
    resp = _client(tmp_path).get("/api/map-tiles/carto-dark/3/4/5.png")
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert resp.data == _PNG


def test_serves_a_jpeg_stored_under_a_png_name_with_jpeg_mime(tmp_path):
    # the cache names every tile *.png but may hold JPEG bytes -> MIME must come from content
    _store(tmp_path, "carto-dark", 3, 4, 5, _JPEG)
    resp = _client(tmp_path).get("/api/map-tiles/carto-dark/3/4/5.png")
    assert resp.status_code == 200
    assert resp.mimetype == "image/jpeg"
    assert resp.data == _JPEG


def test_missing_tile_is_204(tmp_path):
    resp = _client(tmp_path).get("/api/map-tiles/carto-dark/3/4/5.png")
    assert resp.status_code == 204
    assert resp.data == b""


def test_unknown_provider_is_404(tmp_path):
    _store(tmp_path, "carto-dark", 1, 0, 0, _PNG)          # a real tile under a real provider
    resp = _client(tmp_path).get("/api/map-tiles/not-a-provider/1/0/0.png")
    assert resp.status_code == 404


def test_out_of_range_coordinates_are_400(tmp_path):
    client = _client(tmp_path)
    assert client.get("/api/map-tiles/carto-dark/25/0/0.png").status_code == 400   # z > max_zoom
    assert client.get("/api/map-tiles/carto-dark/1/2/0.png").status_code == 400    # x >= 2^z (=2)
    assert client.get("/api/map-tiles/carto-dark/1/0/2.png").status_code == 400    # y >= 2^z


def test_corrupt_cached_content_is_502_not_a_success(tmp_path):
    _store(tmp_path, "carto-dark", 3, 4, 5, b"<html>rate limited</html>")   # in budget, bad magic
    resp = _client(tmp_path).get("/api/map-tiles/carto-dark/3/4/5.png")
    assert resp.status_code == 502
    assert resp.mimetype != "image/png" and resp.status_code not in (200, 204)


def test_over_budget_cached_tile_is_502(tmp_path, monkeypatch):
    monkeypatch.setattr(map_cache_api, "MAX_TILE_BYTES", 16)    # tiny budget for the test
    _store(tmp_path, "carto-dark", 3, 4, 5, _PNG)              # 72 bytes > 16
    resp = _client(tmp_path).get("/api/map-tiles/carto-dark/3/4/5.png")
    assert resp.status_code == 502


def test_no_network_fetch_and_no_cache_write(tmp_path, monkeypatch):
    # any attempt to fetch is a hard failure; a read route must never reach the network.
    def _boom(*a, **k):
        raise AssertionError("the cache route must never fetch")
    monkeypatch.setattr(map_tiles.TileCache, "fetch", _boom)
    monkeypatch.setattr(map_tiles.TileCache, "get_or_fetch", _boom)
    _store(tmp_path, "carto-dark", 2, 1, 1, _PNG)
    before = _files(tmp_path)
    client = _client(tmp_path)
    for path in ("/api/maps/tile-providers",
                 "/api/map-tiles/carto-dark/2/1/1.png",       # cached
                 "/api/map-tiles/carto-dark/2/0/0.png",       # missing
                 "/api/map-tiles/nope/2/1/1.png",             # unknown provider
                 "/api/map-tiles/carto-dark/25/0/0.png"):     # out of range
        client.get(path)
    assert _files(tmp_path) == before                          # nothing written
