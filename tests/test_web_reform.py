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
    # line, bound to the connected device's live runtime_capabilities + telemetry (not invented).
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


def test_quick_commands_returns_real_registry_for_firmware():
    # The OPERATE console loads its command grid from the firmware's own protocol registry (no
    # phantom verbs). Marauder has a rich set; each command carries a danger label for the gate.
    c = _client(DeviceManager())
    data = c.get("/api/quick-commands?firmware=marauder").get_json()
    assert data["firmware"] == "marauder"
    assert data["groups"], "marauder should expose grouped commands"
    cmds = [cmd for g in data["groups"] for cmd in g["commands"]]
    assert any(cmd["command"] == "scanall" for cmd in cmds)
    assert all(set(cmd) == {"command", "label", "danger"} for cmd in cmds)


def test_quick_commands_requires_auth():
    c = _client(DeviceManager(), authed=False)
    assert c.get("/api/quick-commands?firmware=marauder").status_code == 401


def test_gate_status_returns_booleans_never_secrets():
    # SETTINGS reads the access-gate status — booleans + policy only, never a secret byte.
    c = _client(DeviceManager())
    data = c.get("/api/gate-status").get_json()
    expected = {"configured", "policy", "has_password", "has_key", "locked", "remaining_secs"}
    assert set(data) == expected
    assert isinstance(data["configured"], bool)
    # no verifier/secret fields leak
    assert "password" not in data and "key" not in data and "verifier" not in data


def test_gate_status_requires_auth():
    c = _client(DeviceManager(), authed=False)
    assert c.get("/api/gate-status").status_code == 401


def test_crack_tools_is_detection_only():
    # The CRACK card shows engine availability; "native" is always a backend. Detection only — the
    # endpoint never runs a crack (that stays behind the per-run consent gate).
    c = _client(DeviceManager())
    data = c.get("/api/crack-tools").get_json()
    assert "native" in data["backends"]
    assert isinstance(data["tools"], list)


def test_crack_tools_requires_auth():
    c = _client(DeviceManager(), authed=False)
    assert c.get("/api/crack-tools").status_code == 401


def test_nodes_status_fails_closed_locked():
    # With no unlocked vault the Mesh nodes endpoint returns unlocked=false + NO rows (fail-closed);
    # list_rows() is key-redacted so no key byte can leak regardless.
    c = _client(DeviceManager())
    data = c.get("/api/nodes-status").get_json()
    assert data["unlocked"] is False
    assert data["rows"] == []


def test_nodes_status_requires_auth():
    c = _client(DeviceManager(), authed=False)
    assert c.get("/api/nodes-status").status_code == 401


def test_flock_bbox_validation():
    c = _client(DeviceManager())
    assert c.get("/api/flock?bbox=1,2,3").status_code == 400        # not four values
    assert c.get("/api/flock?bbox=a,b,c,d").status_code == 400      # not numbers


def test_flock_imports_cameras(monkeypatch):
    # Endpoint parses a (monkeypatched) Overpass response into map cameras — no live network.
    from src.core import flock_osm

    sample = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-74.0, 40.71]},
         "properties": {"ssid": "CityCam", "mac": "osm:1"}},
    ]}
    monkeypatch.setattr(flock_osm, "fetch_alpr_geojson", lambda bbox, **k: sample)
    c = _client(DeviceManager())
    data = c.get("/api/flock?bbox=40.70,-74.02,40.75,-73.96").get_json()
    assert data["count"] == 1
    assert data["cameras"][0]["lat"] == 40.71 and data["cameras"][0]["lon"] == -74.0
    assert data["attribution"]  # ODbL attribution surfaced


def test_flock_requires_auth():
    c = _client(DeviceManager(), authed=False)
    assert c.get("/api/flock?bbox=0,0,1,1").status_code == 401


def _client_with_pool(pool):
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), pool)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = "tok-xyz"
    return c


def test_targets_clear_empties_the_pool():
    from src.models.target import Target, TargetType

    pool = TargetPool()
    pool.add(Target(mac="AA:BB:CC:DD:EE:01", target_type=TargetType.AP, ssid="net", rssi=-50))
    assert pool.count == 1
    c = _client_with_pool(pool)
    r = c.post("/api/targets/clear", headers={"X-CSRF-Token": "tok-xyz"})
    assert r.status_code == 200
    assert r.get_json()["count"] == 1  # one removed
    assert pool.count == 0


def test_targets_clear_requires_csrf():
    c = _client(DeviceManager())  # authed but no CSRF header
    assert c.post("/api/targets/clear").status_code == 403


def test_targets_export_is_csv_download():
    from src.models.target import Target, TargetType

    pool = TargetPool()
    pool.add(Target(mac="AA:BB:CC:DD:EE:02", target_type=TargetType.AP, ssid="MyNet", rssi=-60))
    c = _client_with_pool(pool)
    r = c.get("/api/targets/export")
    assert r.status_code == 200
    assert "text/csv" in r.content_type
    assert "attachment" in r.headers.get("Content-Disposition", "")
    assert "MyNet" in r.get_data(as_text=True)


def test_targets_export_requires_auth():
    c = _client(DeviceManager(), authed=False)
    assert c.get("/api/targets/export").status_code == 401


def test_macros_list_is_display_only_no_path_leak():
    # The Macros card lists saved macros as display metadata — never the filesystem path.
    c = _client(DeviceManager())
    r = c.get("/api/macros")
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, list)
    for m in data:
        assert set(m) <= {"name", "step_count", "protocol", "secured", "offensive"}
        assert "path" not in m  # server path never crosses the wire


def test_macros_requires_auth():
    c = _client(DeviceManager(), authed=False)
    assert c.get("/api/macros").status_code == 401


class _FakeConn:
    is_connected = True

    def __init__(self):
        self.writes = []

    def write(self, s):
        self.writes.append(s)


def _macro_run_client(tmp_path):
    from src.core.macro_recorder import Macro, MacroRecorder, MacroStep

    rec = MacroRecorder(macros_dir=tmp_path)
    rec.save_macro(Macro(name="Recon Sweep", steps=[MacroStep(command="scanall")],
                         device_protocol="marauder"))
    rec.save_macro(Macro(name="[TEMPLATE] Deauth", steps=[MacroStep(command="attack -t deauth")],
                         device_protocol="marauder"))
    dm = DeviceManager()
    dm.add_device(Device(port="COM9", name="M", firmware="marauder", connected=True))
    dm._connections["COM9"] = _FakeConn()
    app, _sio = create_app(dm, FlashEngine(), EventBus(), TargetPool(), macro_recorder=rec)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = "tok"
    return c


def test_macro_run_offensive_refused_without_consent(tmp_path):
    # THE safety gate: a transmitting/offensive macro is refused (403) unless authorized-use is
    # confirmed; the engine ALSO hard-refuses (armed=False) — defense in depth.
    c = _macro_run_client(tmp_path)
    r = c.post("/api/macros/run", json={"name": "[TEMPLATE] Deauth", "port": "COM9"},
               headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 403


def test_macro_run_offensive_allowed_with_consent(tmp_path):
    c = _macro_run_client(tmp_path)
    body = {"name": "[TEMPLATE] Deauth", "port": "COM9", "consent": True}
    r = c.post("/api/macros/run", json=body, headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 202


def test_macro_run_recon_no_consent_needed(tmp_path):
    c = _macro_run_client(tmp_path)
    r = c.post("/api/macros/run", json={"name": "Recon Sweep", "port": "COM9"},
               headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 202


def test_macro_run_requires_csrf():
    c = _client(DeviceManager())
    assert c.post("/api/macros/run").status_code == 403


def test_macro_run_requires_auth():
    c = _client(DeviceManager(), authed=False)
    assert c.post("/api/macros/run").status_code == 401


def _rules_client():
    from src.core.cross_comm import AutoRouter

    bus = EventBus()
    router = AutoRouter(bus, lambda port, command: None)
    app, _sio = create_app(DeviceManager(), FlashEngine(), bus, TargetPool(bus), auto_router=router)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = "tok"
    return c, router


_HDR = {"X-CSRF-Token": "tok"}


def test_rule_add_offensive_refused_without_consent():
    c, _ = _rules_client()
    r = c.post("/api/rules",
               json={"name": "d", "command_template": "attack -t deauth", "device_port": "COM4"},
               headers=_HDR)
    assert r.status_code == 403


def test_rule_add_offensive_lands_disabled_with_consent():
    # Even consented, an offensive rule is added DISABLED — it can never auto-fire on add.
    c, router = _rules_client()
    r = c.post("/api/rules",
               json={"name": "d", "command_template": "attack -t deauth", "device_port": "COM4",
                     "consent": True},
               headers=_HDR)
    assert r.status_code == 201
    assert r.get_json()["enabled"] is False
    assert all(not rr.enabled for rr in router.list_rules() if rr.name == "d")


def test_rule_add_recon_enabled():
    c, _ = _rules_client()
    r = c.post("/api/rules",
               json={"name": "s", "command_template": "scanall", "device_port": "COM4"}, headers=_HDR)
    assert r.status_code == 201
    assert r.get_json()["enabled"] is True


def test_rule_arm_offensive_needs_consent():
    c, _ = _rules_client()
    c.post("/api/rules",
           json={"name": "d", "command_template": "attack -t deauth", "device_port": "COM4",
                 "consent": True}, headers=_HDR)
    # enabling (arming) the offensive rule without consent is refused
    r = c.post("/api/rules/toggle", json={"name": "d", "enabled": True}, headers=_HDR)
    assert r.status_code == 403
    # with consent it arms
    r2 = c.post("/api/rules/toggle", json={"name": "d", "enabled": True, "consent": True}, headers=_HDR)
    assert r2.status_code == 200


def test_rules_list_and_remove():
    c, _ = _rules_client()
    c.post("/api/rules",
           json={"name": "s", "command_template": "scanall", "device_port": "COM4"}, headers=_HDR)
    assert any(x["name"] == "s" for x in c.get("/api/rules").get_json())
    assert c.post("/api/rules/remove", json={"name": "s"}, headers=_HDR).get_json()["status"] == "removed"


def test_rules_require_csrf():
    c = _client(DeviceManager())
    assert c.post("/api/rules").status_code == 403


def _client_with_desktop_token(token):
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool(),
                           desktop_token=token)
    return app.test_client()


def test_desktop_auth_bootstraps_a_clean_session():
    # The one-time token establishes a session and 302s to /reform WITHOUT credentials in the URL,
    # so the window lands on a clean address where relative fetch() works.
    c = _client_with_desktop_token("secret-token-xyz")
    r = c.get("/desktop-auth?token=secret-token-xyz")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/reform")
    # session is now authenticated — /reform renders without a 401
    assert c.get("/reform").status_code == 200


def test_desktop_auth_is_single_use():
    c = _client_with_desktop_token("one-shot")
    assert c.get("/desktop-auth?token=one-shot").status_code == 302
    # the token is consumed after one use — the route goes inert (404) so it can't be replayed
    fresh = c.application.test_client()
    assert fresh.get("/desktop-auth?token=one-shot").status_code == 404


def test_desktop_auth_rejects_bad_token():
    c = _client_with_desktop_token("right")
    assert c.get("/desktop-auth?token=wrong").status_code == 403


def test_desktop_auth_inert_without_token():
    # The LAN web server never sets a desktop token, so the route must not exist (no auth bypass).
    c = _client(DeviceManager(), authed=False)
    assert c.get("/desktop-auth?token=anything").status_code == 404


def test_desktop_shell_module_importable():
    # The pywebview shell must import cleanly (its webview dep is optional and imported lazily
    # inside launch_desktop, so importing the module never requires pywebview to be installed).
    from src.ui.web import desktop

    assert callable(desktop.launch_desktop)
