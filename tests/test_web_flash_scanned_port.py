"""Web /api/flash must accept a port that the Flash page itself shows.

Regression: ``flash_page()`` builds its Port dropdown from a LIVE ``device_manager.scan_ports()``
enumeration, but ``api_flash()`` gated on ``_known_port()``, which only consulted the DeviceManager
registry. A device already plugged in when the web server started is seeded into
``HotPlugMonitor._known_ports`` WITHOUT ever being ``add_device``'d, so it is never registered — and
/api/flash rejected the very port the user just selected with 400 'Unknown/unregistered port'.

``_known_port`` now also honours a fresh scan (the same source the page renders from), while still
rejecting a port that does not physically exist.
"""
from __future__ import annotations

import pytest

pytest.importorskip("flask")

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.models.device import Device
from src.ui.web.app import create_app


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")


def _client(dm):
    app, _sio = create_app(dm, FlashEngine(), EventBus(), TargetPool())
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = "tok"
    return client


def _post_flash(client, port, profile_id):
    return client.post(
        "/api/flash",
        json={"port": port, "profile_id": profile_id, "_csrf": "tok"},
        headers={"X-CSRF-Token": "tok"},
    )


def test_flash_accepts_startup_present_scanned_port(monkeypatch):
    # Registry is EMPTY (mirrors a device plugged in before the server started, never add_device'd),
    # but the live scan sees COM5 — exactly what /flash renders in its dropdown.
    dm = DeviceManager()
    monkeypatch.setattr(dm, "scan_ports", lambda: [Device(port="COM5", name="ESP32-Marauder")])
    assert dm.list_devices() == []  # confirm nothing is registered

    resp = _post_flash(_client(dm), "COM5", "no-such-profile-xyz")
    # The port passes the registry gate now, so the request proceeds to the profile check and 404s on the
    # unknown profile. BEFORE the fix this returned 400 'Unknown/unregistered port: COM5'.
    assert resp.status_code == 404
    assert "Unknown profile" in resp.get_json()["error"]


def test_flash_still_rejects_absent_port(monkeypatch):
    # Guard against a naive accept-all: a port that is neither registered NOR present in a live scan is
    # still rejected. Only COM5 is scannable; COM9 does not exist anywhere.
    dm = DeviceManager()
    monkeypatch.setattr(dm, "scan_ports", lambda: [Device(port="COM5", name="ESP32-Marauder")])

    resp = _post_flash(_client(dm), "COM9", "no-such-profile-xyz")
    assert resp.status_code == 400
    assert "Unknown/unregistered port" in resp.get_json()["error"]
