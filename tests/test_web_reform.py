"""Reform web shell (GUI-stack pivot, Phase-1 proof) — /reform renders the approved mockup from CC's
own core, DEVICE ▸ Dashboard wired to live data, and /api/system-health feeds the gauges. Auth-gated
like the rest of the web UI. Guards the route + endpoint + the pywebview desktop module so the pivot
can't silently regress."""
from __future__ import annotations

import pytest

pytest.importorskip("flask")

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.models.device import Device
from src.security.web_auth import new_csrf_token
from src.ui.web.app import create_app


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    # Isolate the gate config so a test never touches the real machine gate.
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")


def _client(dm, authed=True):
    app, _sio = create_app(dm, FlashEngine(), EventBus(), TargetPool())
    c = app.test_client()
    if authed:
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["csrf"] = new_csrf_token()
    return c


def test_reform_requires_auth():
    c = _client(DeviceManager(), authed=False)
    assert c.get("/reform").status_code == 401


def test_reform_renders_the_shell_and_live_regions():
    c = _client(DeviceManager())
    body = c.get("/reform").get_data(as_text=True)
    assert c.get("/reform").status_code == 200
    # the mockup shell chrome
    for needle in ("DEVICE", "HUNT", "OPERATE", "CRACK", "MAP", "System Health", "Cross-Comm"):
        assert needle in body, f"missing shell element: {needle}"
    # live-wired assets + hydration hooks (not a static HTML dump)
    for needle in ("reform.css", "reform.js", 'data-metric="cpu"', "dash-devices", "dash-pool"):
        assert needle in body, f"missing live hook: {needle}"


def test_reform_lists_connected_device():
    dm = DeviceManager()
    dm.add_device(Device(port="COM9", name="Marauder", firmware="marauder", connected=True))
    body = _client(dm).get("/reform").get_data(as_text=True)
    assert "COM9" in body


def test_reform_selected_device_card_binds_live_fields():
    # The Selected Device card mirrors the mockup: capability chips + a board/fw/ui/ops/heap detail
    # line, both bound to the connected device's live runtime_capabilities + telemetry (not invented).
    dm = DeviceManager()
    dev = Device(port="COM9", name="Marauder", firmware="marauder", connected=True, health="alive")
    dev.runtime_capabilities = frozenset({"wifi", "ble"})
    dev.telemetry = {"board": "esp32-s3", "fw": "v1.5b", "heap": 214 * 1024}
    dm.add_device(dev)
    body = _client(dm).get("/reform").get_data(as_text=True)
    assert "WIFI" in body and "BLE" in body  # capability chips
    assert "esp32-s3" in body and "fw v1.5b" in body and "heap 214 KB" in body  # detail line


def test_system_health_endpoint_shape():
    c = _client(DeviceManager())
    r = c.get("/api/system-health")
    assert r.status_code == 200
    data = r.get_json()
    for key in ("cpu_percent", "memory_percent", "disk_percent", "gps_fix"):
        assert key in data
    assert r.headers.get("Cache-Control") == "no-store"


def test_system_health_requires_auth():
    c = _client(DeviceManager(), authed=False)
    assert c.get("/api/system-health").status_code == 401


def test_desktop_shell_module_importable():
    # The pywebview shell must import cleanly (its webview dep is optional and imported lazily
    # inside launch_desktop, so importing the module never requires pywebview to be installed).
    from src.ui.web import desktop

    assert callable(desktop.launch_desktop)
