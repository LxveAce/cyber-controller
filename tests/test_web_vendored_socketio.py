"""The Socket.IO client is vendored + served same-origin instead of pulled from cdnjs with no SRI:
no supply-chain surface from a compromised CDN, works offline, and the CSP has no external script origin.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.core.resources import resource_path
from src.ui.web import app as webapp

_WEB = Path(__file__).resolve().parents[1] / "src" / "ui" / "web"


@pytest.fixture()
def client():
    app, _sio = webapp.create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool(EventBus()))
    app.config.update(TESTING=True)
    return app.test_client()


def test_no_cdn_reference_in_base_template():
    base = (_WEB / "templates" / "base.html").read_text(encoding="utf-8")
    assert "cdnjs" not in base and "cloudflare" not in base, "base.html must not load scripts from a CDN"
    assert "vendor/socket.io.min.js" in base, "base.html must load the vendored socket.io"


def test_vendored_file_present_and_served_same_origin(client):
    p = resource_path("src", "ui", "web", "static", "vendor", "socket.io.min.js")
    assert p.is_file() and p.stat().st_size > 10000, "vendored socket.io must be bundled"
    r = client.get("/static/vendor/socket.io.min.js")
    assert r.status_code == 200 and b"Socket.IO" in r.data


def test_csp_has_no_external_script_origin(client):
    r = client.get("/static/vendor/socket.io.min.js")
    csp = r.headers.get("Content-Security-Policy", "")
    assert "cdnjs" not in csp and "cloudflare" not in csp, "CSP must not allow an external script origin"
    assert "script-src 'self' 'nonce-" in csp


def test_service_worker_precaches_vendored_socketio():
    sw = (_WEB / "static" / "sw.js").read_text(encoding="utf-8")
    assert "/static/vendor/socket.io.min.js" in sw, "SW should precache the vendored client for offline"
