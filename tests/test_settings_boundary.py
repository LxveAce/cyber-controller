"""F03: saved serial/flash baud must reach the operation boundary (connect / flash), not just round-trip
through settings JSON. Mocked device/flash — no real serial open or flash."""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("flask")

from src.config import settings as app_settings
from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.models.device import Device
from src.security.web_auth import new_csrf_token
from src.ui.web.app import create_app


def _client(monkeypatch, tmp_path, dm, engine):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")
    app, _sio = create_app(dm, engine, EventBus(), TargetPool())
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = new_csrf_token()
        sess["_csrf"] = sess["csrf"]
    return client


def test_connect_applies_saved_serial_baud(monkeypatch, tmp_path):
    monkeypatch.setattr(app_settings, "load_settings",
                        lambda: {"serial": {"default_baud": 9600}, "flash": {"flash_baud": 921600}})
    dm = DeviceManager()
    captured = {}

    def fake_open(port, baud=115200, owner=None):
        captured["baud"] = baud
        raise RuntimeError("stop after capturing baud")   # short-circuit the post-connect probe/GPS path

    monkeypatch.setattr(dm, "get_device", lambda p: Device(port=p, connected=False))
    monkeypatch.setattr(dm, "open_connection", fake_open)
    monkeypatch.setattr(FlashEngine, "is_port_busy", staticmethod(lambda port: False))

    client = _client(monkeypatch, tmp_path, dm, FlashEngine())
    with client.session_transaction() as s:
        csrf = s["csrf"]
    client.post("/api/connect", json={"port": "COM9"}, headers={"X-CSRF-Token": csrf})
    assert captured.get("baud") == 9600   # the saved serial baud reached open_connection, not the 115200 default


def test_flash_applies_saved_flash_baud_to_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(app_settings, "load_settings",
                        lambda: {"serial": {"default_baud": 115200}, "flash": {"flash_baud": 115200}})

    class _Profile:
        def __init__(self):
            self.baud = 921600   # a profile default (e.g. Meshtastic)
            self.variant = ""

    prof = _Profile()
    seen = {}
    done = threading.Event()
    engine = FlashEngine()
    monkeypatch.setattr(engine, "load_profile", lambda path: prof)
    monkeypatch.setattr(engine, "is_port_busy", lambda port: False)

    def fake_flash(port, profile, progress_callback=None, **kw):
        seen["baud"] = profile.baud
        done.set()
        return True

    monkeypatch.setattr(engine, "flash", fake_flash)

    dm = DeviceManager()
    monkeypatch.setattr(dm, "get_device", lambda p: Device(port=p, connected=True))
    monkeypatch.setattr(dm, "scan_ports", lambda: [Device(port="COM9", connected=True)])
    monkeypatch.setattr(dm, "close_connection", lambda *a, **k: None)

    client = _client(monkeypatch, tmp_path, dm, engine)
    with client.session_transaction() as s:
        csrf = s["csrf"]
    resp = client.post("/api/flash", json={"port": "COM9", "profile_id": "ESP32 Marauder"},
                       headers={"X-CSRF-Token": csrf})
    # the flash runs in a thread; wait briefly for it to record the effective baud
    assert done.wait(3.0), f"flash never ran (status {resp.status_code})"
    assert seen["baud"] == 115200   # the saved flash baud overrode the profile's 921600 default


def test_auto_flash_baud_leaves_the_profile_baud_alone(monkeypatch, tmp_path):
    # F03 precedence: flash_baud None == "Auto" — the firmware profile's own baud must be used, NOT silently
    # overridden. A profile that pins a lower baud for reliability would break if the global default won.
    monkeypatch.setattr(app_settings, "load_settings",
                        lambda: {"serial": {"default_baud": 115200}, "flash": {"flash_baud": None}})

    class _Profile:
        def __init__(self):
            self.baud = 460800   # a profile that pins a specific baud
            self.variant = ""

    prof = _Profile()
    seen = {}
    done = threading.Event()
    engine = FlashEngine()
    monkeypatch.setattr(engine, "load_profile", lambda path: prof)
    monkeypatch.setattr(engine, "is_port_busy", lambda port: False)

    def fake_flash(port, profile, progress_callback=None, **kw):
        seen["baud"] = profile.baud
        done.set()
        return True

    monkeypatch.setattr(engine, "flash", fake_flash)

    dm = DeviceManager()
    monkeypatch.setattr(dm, "get_device", lambda p: Device(port=p, connected=True))
    monkeypatch.setattr(dm, "scan_ports", lambda: [Device(port="COM9", connected=True)])
    monkeypatch.setattr(dm, "close_connection", lambda *a, **k: None)

    client = _client(monkeypatch, tmp_path, dm, engine)
    with client.session_transaction() as s:
        csrf = s["csrf"]
    resp = client.post("/api/flash", json={"port": "COM9", "profile_id": "ESP32 Marauder"},
                       headers={"X-CSRF-Token": csrf})
    assert done.wait(3.0), f"flash never ran (status {resp.status_code})"
    assert seen["baud"] == 460800   # Auto (None) left the profile's own baud untouched
