"""Web remote UI-audit Batch UI-1 regressions (run wf_3f2a4b2c-b41).

Three confirmed defects on the Flask web surface:

* app.py:447 — /api/flash never released an already-open managed serial connection before shelling
  esptool, and /api/connect + /api/command had no busy-guard, so a monitor connection (or a fresh
  connect/command) contended with esptool over the same UART (Windows "Access denied" flash-fail;
  POSIX concurrent read -> corrupt sync / brick risk).
* app.py:314/657/667 — the dashboard, /api/devices and /api/health counted only the raw registry,
  which is empty for a board plugged in before the server started, so they reported "0 devices"
  while /devices (which uses the merged scan) correctly showed the board.
"""
from __future__ import annotations

import pytest

pytest.importorskip("flask")

from flask import session

import src.ui.web.app as webapp
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


def _client(dm, fe=None):
    app, _sio = create_app(dm, fe or FlashEngine(), EventBus(), TargetPool())
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = "tok"
    return client


def _csrf(client, path, body):
    return client.post(path, json={**body, "_csrf": "tok"}, headers={"X-CSRF-Token": "tok"})


# ── device-count: a board present before server start is counted, not "0 devices" ────────────────

def test_api_devices_and_health_count_startup_present_port(monkeypatch):
    # Empty registry (never add_device'd), but the live scan sees COM5 — the pre-startup-plugged case.
    dm = DeviceManager()
    monkeypatch.setattr(dm, "scan_ports", lambda: [Device(port="COM5", name="ESP32-Marauder")])
    assert dm.list_devices() == []
    client = _client(dm)

    listing = client.get("/api/devices")
    assert listing.status_code == 200
    assert any(d["port"] == "COM5" for d in listing.get_json())  # was [] before the fix

    health = client.get("/api/health").get_json()
    assert health["device_count"] == 1  # was 0 before the fix
    assert health["connected_count"] == 0  # scanned-but-unconnected reads present-not-connected

    dash = client.get("/")
    assert dash.status_code == 200


# ── flash releases a held UART, and connect/command 409 while a flash is running ──────────────────

def test_flash_releases_managed_connection_before_esptool(monkeypatch):
    dm = DeviceManager()
    monkeypatch.setattr(dm, "scan_ports", lambda: [Device(port="COM5", name="ESP32-Marauder")])
    closed: list[str] = []
    monkeypatch.setattr(dm, "close_connection", lambda port, owner=None: closed.append(port))

    fe = FlashEngine()
    monkeypatch.setattr(fe, "load_profile", lambda path: object())
    monkeypatch.setattr(fe, "flash", lambda *a, **k: True)  # no real esptool
    monkeypatch.setattr(webapp, "_load_profiles", lambda: {"test-fw": webapp.Path("x.json")})

    resp = _csrf(_client(dm, fe), "/api/flash", {"port": "COM5", "profile_id": "test-fw"})
    assert resp.status_code == 200
    # The managed connection on COM5 is force-released (no owner) synchronously, before the flash thread
    # can hand the UART to esptool. Before the fix nothing released it.
    assert closed == ["COM5"]


def test_connect_rejected_while_port_is_flashing(monkeypatch):
    dm = DeviceManager()
    monkeypatch.setattr(dm, "scan_ports", lambda: [Device(port="COM5", name="ESP32-Marauder")])
    fe = FlashEngine()
    monkeypatch.setattr(fe, "is_port_busy", lambda port: port == "COM5")

    resp = _csrf(_client(dm, fe), "/api/connect", {"port": "COM5"})
    assert resp.status_code == 409
    assert "busy" in resp.get_json()["error"].lower()


def test_command_rejected_while_port_is_flashing(monkeypatch):
    dm = DeviceManager()
    monkeypatch.setattr(dm, "scan_ports", lambda: [Device(port="COM5", name="ESP32-Marauder")])
    fe = FlashEngine()
    monkeypatch.setattr(fe, "is_port_busy", lambda port: port == "COM5")

    resp = _csrf(_client(dm, fe), "/api/command", {"port": "COM5", "command": "help"})
    assert resp.status_code == 409
    assert "busy" in resp.get_json()["error"].lower()


def _capture_socket_handlers(monkeypatch) -> dict:
    """Stash the raw ``@socketio.on`` closures by name so we can drive one directly (SocketIO.on's
    decorator returns the original handler, so we wrap it to capture the reference)."""
    captured: dict = {}
    orig_on = webapp.SocketIO.on

    def patched_on(self, message, namespace=None):
        deco = orig_on(self, message, namespace=namespace)

        def capturing(handler):
            captured[message] = handler
            return deco(handler)

        return capturing

    monkeypatch.setattr(webapp.SocketIO, "on", patched_on)
    return captured


def test_ws_send_command_rejected_while_port_is_flashing(monkeypatch):
    # The interactive /terminal Socket.IO path must refuse to write mid-flash, like its HTTP twin
    # /api/command — a stray byte during esptool can brick the board.
    captured = _capture_socket_handlers(monkeypatch)
    emits: list = []
    monkeypatch.setattr(webapp, "emit", lambda ev, payload=None, **k: emits.append(payload))

    writes: list = []

    class _Conn:
        is_connected = True

        def write(self, data):  # must never be reached while the port is flashing
            writes.append(data)

    dm = DeviceManager()
    dm.add_device(Device(port="COM5", name="Marauder", firmware="marauder", connected=True))
    monkeypatch.setattr(dm, "get_connection", lambda p: _Conn() if p == "COM5" else None)
    fe = FlashEngine()
    monkeypatch.setattr(fe, "is_port_busy", lambda port: port == "COM5")

    app, _sio = webapp.create_app(dm, fe, EventBus(), TargetPool())
    send_command = captured["send_command"]

    with app.test_request_context(environ_base={"REMOTE_ADDR": "10.0.0.1"}):
        session["authenticated"] = True
        send_command({"port": "COM5", "command": "help"})

    assert writes == []  # the guard returned before any byte hit the UART
    busy = [p for p in emits if p and "busy" in str(p.get("line", "")).lower()]
    assert busy and busy[0]["port"] == "COM5"  # operator saw the busy notice, not a silent drop
