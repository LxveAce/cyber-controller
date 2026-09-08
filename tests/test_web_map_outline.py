"""Route tests for GET /api/maps/world-outline (offline-maps overview).

These exercise the actual ``_world_outline_response`` handler and its bundled-asset resolution in a
minimal in-process Flask app that registers only this one handler over temporary (or the real
bundled) resources. They do NOT build the real CC runtime, managers, sockets, or owner state.

Scope is deliberately narrow: resource lookup, content type, the recoverable-missing response, and
conditional caching. The ``@requires_auth`` wrapper, the app lifecycle, and frozen-artifact
resolution are outside this inert test and are not qualified here.
"""
from __future__ import annotations

import hashlib
import json

from flask import Flask

from src.ui.web import app as webapp

_ASSET = "world_110m.geojson"


def _client(maps_dir):
    """A bare Flask app with ONLY the world-outline handler registered (no auth, no lifecycle)."""
    app = Flask(__name__)
    app.add_url_rule(
        "/api/maps/world-outline", "world_outline",
        lambda: webapp._world_outline_response(maps_dir),
    )
    return app.test_client()


def test_serves_geojson_from_a_temp_fixture(tmp_path):
    data = b'{"type":"FeatureCollection","features":[]}'
    (tmp_path / _ASSET).write_bytes(data)
    resp = _client(tmp_path).get("/api/maps/world-outline")
    assert resp.status_code == 200
    assert resp.mimetype == "application/geo+json"
    assert resp.data == data


def test_missing_asset_is_recoverable_unavailable(tmp_path):
    # empty dir -> asset absent
    resp = _client(tmp_path).get("/api/maps/world-outline")
    assert resp.status_code == 503                      # ordinary recoverable "unavailable"
    assert resp.mimetype == "application/json"
    assert resp.get_json().get("error")                 # a message, not a fabricated success
    assert resp.status_code != 200


def test_serves_the_actual_bundled_world_outline():
    # default maps_dir -> resource_path("src","config","maps"); the real bundled asset.
    resp = _client(None).get("/api/maps/world-outline")
    assert resp.status_code == 200
    assert resp.mimetype == "application/geo+json"
    # exact bundled bytes and full SHA-256 identity, unchanged.
    on_disk = (webapp._MAPS_DIR / _ASSET).read_bytes()
    assert resp.data == on_disk
    assert hashlib.sha256(resp.data).hexdigest() == (
        "e6d80e0d1b3095271ebbdc6d6b89098d5c54e1c676a524619667ca8eb9391016"
    )
    # valid GeoJSON FeatureCollection, Natural Earth 1:110m admin-0 (177 country features).
    doc = json.loads(resp.data)
    assert doc["type"] == "FeatureCollection"
    assert len(doc["features"]) == 177


def test_conditional_get_returns_304(tmp_path):
    (tmp_path / _ASSET).write_bytes(b'{"type":"FeatureCollection","features":[]}')
    client = _client(tmp_path)
    first = client.get("/api/maps/world-outline")
    etag = first.headers.get("ETag")
    assert etag, "send_from_directory should set an ETag for conditional caching"
    again = client.get("/api/maps/world-outline", headers={"If-None-Match": etag})
    assert again.status_code == 304                     # not re-sent when unchanged


def test_no_user_supplied_path_reaches_the_route(tmp_path):
    # The route takes NO path/provider from the request; a query string is ignored (fixed asset).
    (tmp_path / _ASSET).write_bytes(b'{"type":"FeatureCollection","features":[]}')
    resp = _client(tmp_path).get("/api/maps/world-outline?path=/etc/passwd&provider=x")
    assert resp.status_code == 200
    assert resp.mimetype == "application/geo+json"
