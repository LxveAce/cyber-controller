"""Web serial-command surface must be reachable — /devices offers a Connect action and /api/connect
opens a managed connection so /api/command can reach a real device.

Regression: nothing in the web layer ever called ``device_manager.open_connection``. ``get_connection()``
was therefore always None, so ``/api/command`` / ``send_command`` always returned 'No active connection',
and /devices showed 'No devices detected' for a device present at startup (never registered) — with no UI
control anywhere to open the link. The entire serial-command path of the web remote was unreachable.

The fix adds POST /api/connect (register scanned Device + open_connection) and /api/disconnect, plus
Connect/Disconnect buttons on /devices, so a connection can exist before the command path is used.
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


class _FakeConn:
    """Stands in for a live SerialConnection so we don't touch real hardware."""

    is_connected = True

    def __init__(self):
        self.writes: list[str] = []

    def write(self, s):
        self.writes.append(s)


def _client(dm):
    app, _sio = create_app(dm, FlashEngine(), EventBus(), TargetPool())
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = "tok"
    return client


def _post(client, path, body):
    body = dict(body, _csrf="tok")
    return client.post(path, json=body, headers={"X-CSRF-Token": "tok"})


def test_devices_page_lists_scanned_startup_port_with_connect(monkeypatch):
    # Empty registry + a live-scanned COM5 (device present at startup). The page must show it with a
    # Connect action rather than reading 'No devices detected'.
    dm = DeviceManager()
    monkeypatch.setattr(dm, "scan_ports", lambda: [Device(port="COM5", name="ESP32")])
    body = _client(dm).get("/devices").get_data(as_text=True)

    assert "No devices detected" not in body
    assert "COM5" in body
    assert "btn-connect" in body and 'data-port="COM5"' in body


def test_connect_registers_opens_and_command_reaches_connection(monkeypatch):
    dm = DeviceManager()
    monkeypatch.setattr(dm, "scan_ports", lambda: [Device(port="COM5", name="ESP32")])

    fake = _FakeConn()

    def _open(port, baud=115200, owner=None):
        # Emulate a successful serial open: register the live connection + mark the device connected,
        # exactly as the real open_connection would (without opening a real COM port).
        dm._connections[port] = fake
        dm._devices[port].connected = True
        return fake

    monkeypatch.setattr(dm, "open_connection", _open)

    client = _client(dm)

    # 1) Connect: registers the scanned device and opens the link. (Before the fix this route did not
    #    exist -> 404, so the connection could never be established from the web UI.)
    resp = _post(client, "/api/connect", {"port": "COM5"})
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "connected"
    assert dm.get_device("COM5") is not None
    assert dm.get_connection("COM5") is fake

    # 2) The command path now reaches a live connection instead of 'No active connection'.
    resp = _post(client, "/api/command", {"port": "COM5", "command": "reboot"})
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "sent"
    assert fake.writes == ["reboot"], "the command must actually reach the opened serial connection"


def test_command_without_connect_still_reports_no_connection(monkeypatch):
    # A scanned-but-not-connected port is a known port (finding 1) yet has no live connection until the
    # user connects — so the command path must say so, not silently pretend it worked.
    dm = DeviceManager()
    monkeypatch.setattr(dm, "scan_ports", lambda: [Device(port="COM5", name="ESP32")])
    resp = _post(_client(dm), "/api/command", {"port": "COM5", "command": "reboot"})
    assert resp.status_code == 400
    assert "No active connection" in resp.get_json()["error"]


def test_connect_unknown_port_rejected(monkeypatch):
    dm = DeviceManager()
    monkeypatch.setattr(dm, "scan_ports", lambda: [])  # nothing present
    resp = _post(_client(dm), "/api/connect", {"port": "COM9"})
    assert resp.status_code == 400
    assert "Unknown/unregistered port" in resp.get_json()["error"]
