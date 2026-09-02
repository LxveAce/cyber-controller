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
    # the Selected-Device armed lamp only renders when a device is selected (F2 hook)
    assert 'id="armlamp-sel"' in body


def test_reform_polish_batch_honesty_and_wiring():
    # Guards the 2026-08-08 get-everything polish batch so it can't silently regress.
    body = _client(DeviceManager()).get("/reform").get_data(as_text=True)
    # F2 armed-state: always-present lamps carry ids + a .lt span so JS can flip SAFE<->ARMED
    for needle in ('id="lamp-top"', 'id="armlamp-op"', 'class="lt"'):
        assert needle in body, f"armed-state hook missing: {needle}"
    # F5: the OS button no longer claims to flash from the web UI
    assert "Flash OS" not in body and "Get flash command" in body
    # phantom "Export…" affordance removed from the Captured Handshakes header
    assert "Export&#8230;" not in body and "Export…" not in body
    # wardrive maps are labelled as illustration/sample, not passed off as live telemetry
    assert body.count(">sample<") >= 2
    # card2 empty-state panels use the component class, not the duplicated inline style string
    assert 'class="card2"' in body
    assert "text-align:center;color:var(--mut);padding:22px;border:1px dashed" not in body
    # OPERATE command grid gets the flexible track so its auto-fill grid can form >1 column
    assert 'class="split ops"' in body


def test_reform_a11y_and_inert_toggle_removal():
    # Guards the 2026-08-24 sweep fixes so they can't silently regress.
    body = _client(DeviceManager()).get("/reform").get_data(as_text=True)
    # Honesty: the two inert flash toggles (no consumer in flash_engine) are gone, like the Qt tab.
    assert "set-flash-verify" not in body and "set-flash-backup" not in body
    assert "Verify after flash" not in body and "Back up flash before write" not in body
    # A11y semantics in the template (roving-tabindex + roles are added by reform.js at runtime).
    assert 'class="visually-hidden">Cyber Controller</h1>' in body  # one real h1
    assert 'id="lamp-top" role="status"' in body                   # SAFE<->ARMED flip is announced
    assert 'id="armlamp-op" role="status"' in body
    assert body.count('role="status"') >= 2  # top + OPERATE lamp always render
    assert 'rel="icon"' in body and "favicon.svg" in body            # favicon parity with base.html
    assert 'name="theme-color"' in body
    # Rescan is a real button, not an <a href="#"> (keyboard + role).
    assert 'id="os-rescan" class="linkbtn"' in body
    assert 'href="#" id="os-rescan"' not in body


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
    # phantom verbs), folded into the canonical Scanning/Attack/Network/Other buckets (A16). Each
    # command carries a danger label for the gate + its native category as a sub-label.
    c = _client(DeviceManager())
    data = c.get("/api/quick-commands?firmware=marauder").get_json()
    assert data["firmware"] == "marauder"
    assert data["groups"], "marauder should expose grouped commands"
    # groups are the canonical buckets, never a raw firmware category
    assert all(g["category"] in {"Scanning", "Attack", "Network", "Other"} for g in data["groups"])
    cmds = [cmd for g in data["groups"] for cmd in g["commands"]]
    assert any(cmd["command"] == "scanall" for cmd in cmds)
    assert all(set(cmd) == {"command", "label", "danger", "native"} for cmd in cmds)


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


# ── OPERATE Broadcast fan-out (A16) — one command → many devices, doubly gated when offensive ──
def _broadcast_client():
    dm = DeviceManager()
    conns = {}
    for port in ("COM9", "COM10"):
        dm.add_device(Device(port=port, name="M", firmware="marauder", connected=True))
        conns[port] = _FakeConn()
        dm._connections[port] = conns[port]
    # COM11 is registered but has NO active connection (per-port skip path)
    dm.add_device(Device(port="COM11", name="M", firmware="marauder", connected=False))
    app, _sio = create_app(dm, FlashEngine(), EventBus(), TargetPool())
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = "tok"
    return c, conns


def test_broadcast_requires_auth():
    c = _client(DeviceManager(), authed=False)
    assert c.post("/api/broadcast").status_code == 401


def test_broadcast_requires_csrf():
    assert _client(DeviceManager()).post("/api/broadcast").status_code == 403


def test_broadcast_rejects_empty_ports():
    c, _ = _broadcast_client()
    r = c.post("/api/broadcast", json={"command": "scanall", "ports": []},
               headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 400


def test_broadcast_recon_fans_out_to_all_connected():
    c, conns = _broadcast_client()
    r = c.post("/api/broadcast", json={"command": "scanall", "ports": ["COM9", "COM10"]},
               headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["offensive"] is False and body["sent"] == 2 and body["failed"] == 0
    # the command actually reached both live connections
    assert conns["COM9"].writes == ["scanall"] and conns["COM10"].writes == ["scanall"]


def test_broadcast_offensive_refused_without_consent():
    c, conns = _broadcast_client()
    r = c.post("/api/broadcast", json={"command": "attack -t deauth", "ports": ["COM9", "COM10"]},
               headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 403
    # nothing was transmitted — the gate fired BEFORE any write
    assert conns["COM9"].writes == [] and conns["COM10"].writes == []


def test_broadcast_offensive_allowed_with_consent():
    c, conns = _broadcast_client()
    r = c.post("/api/broadcast",
               json={"command": "attack -t deauth", "ports": ["COM9"], "consent": True},
               headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["offensive"] is True and body["sent"] == 1
    assert conns["COM9"].writes == ["attack -t deauth"]


def test_broadcast_skips_disconnected_port_without_failing_batch():
    c, conns = _broadcast_client()
    r = c.post("/api/broadcast", json={"command": "scanall", "ports": ["COM9", "COM11"]},
               headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["sent"] == 1 and body["failed"] == 1
    results = {x["port"]: x for x in body["results"]}
    assert results["COM9"]["status"] == "sent"
    assert "error" in results["COM11"]          # disconnected → per-port skip, not a 500
    assert conns["COM9"].writes == ["scanall"]


def test_broadcast_metadata_danger_verb_is_gated_like_rules():
    # startportal carries its danger in CommandInfo metadata (classify()=='' for the bare string);
    # the shared _command_is_offensive floor must still gate it — parity with the rules gate.
    c, conns = _broadcast_client()
    r = c.post("/api/broadcast", json={"command": "startportal", "ports": ["COM9"]},
               headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 403
    assert conns["COM9"].writes == []


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
               json={"name": "s", "command_template": "scanall", "device_port": "COM4"},
               headers=_HDR)
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
    r2 = c.post("/api/rules/toggle",
                json={"name": "d", "enabled": True, "consent": True}, headers=_HDR)
    assert r2.status_code == 200


def test_rules_list_and_remove():
    c, _ = _rules_client()
    c.post("/api/rules",
           json={"name": "s", "command_template": "scanall", "device_port": "COM4"}, headers=_HDR)
    assert any(x["name"] == "s" for x in c.get("/api/rules").get_json())
    rm = c.post("/api/rules/remove", json={"name": "s"}, headers=_HDR)
    assert rm.get_json()["status"] == "removed"


def test_rules_require_csrf():
    c = _client(DeviceManager())
    assert c.post("/api/rules").status_code == 403


def test_rule_metadata_offensive_verbs_are_gated():
    # Red-team regression: transmitting verbs whose danger is in metadata (classify returns '' for
    # the bare string) must STILL be treated offensive by the rules gate — else a "subghz tx" /
    # "evilportal" rule would add enabled and auto-fire un-gated.
    for verb in ("subghz tx", "evilportal", "rfid emulate", "startportal"):
        c, _ = _rules_client()
        r = c.post("/api/rules",
                   json={"name": "x", "command_template": verb, "device_port": "COM4"},
                   headers=_HDR)
        assert r.status_code == 403, f"{verb!r} should be refused without consent"


def test_os_images_lists_catalog():
    c = _client(DeviceManager())
    r = c.get("/api/os/images")
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, list) and data  # bundled catalog is non-empty
    assert all("id" in i and "name" in i for i in data)


def test_os_drives_removable_only_shape(monkeypatch):
    # The drive list comes from the hardened removable-only detector; the endpoint exposes device/
    # name/size/bus only. Monkeypatch the detector so the test never depends on real hardware.
    from src.core.backends import sd_backend

    monkeypatch.setattr(sd_backend, "detect_sd_cards",
                        lambda _l: [{"device": "\\\\.\\PhysicalDrive9", "name": "USB", "size": 8e9,
                                     "bus": "USB", "removable": True}])
    c = _client(DeviceManager())
    data = c.get("/api/os/drives").get_json()
    assert data and data[0]["device"] == "\\\\.\\PhysicalDrive9"
    assert set(data[0]) == {"device", "name", "size", "bus"}


def test_os_read_endpoints_require_auth():
    c = _client(DeviceManager(), authed=False)
    assert c.get("/api/os/images").status_code == 401
    assert c.get("/api/os/drives").status_code == 401


def test_every_mutating_route_is_csrf_gated():
    # App-wide security invariant (audit 2026-08-07): EVERY state-changing route (POST/PUT/DELETE)
    # must reject an authed request that carries no CSRF token. Catches a future POST route shipping
    # without @requires_csrf. Parametric paths are skipped (none of ours take a mutating URL arg).
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool())
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True  # authed, but deliberately NO csrf token in the request
    checked = 0
    for rule in app.url_map.iter_rules():
        methods = rule.methods - {"GET", "HEAD", "OPTIONS"}
        if not methods or "{" in rule.rule.replace("<", "{"):  # skip GET-only + parametric paths
            continue
        method = "POST" if "POST" in methods else sorted(methods)[0]
        resp = c.open(rule.rule, method=method)
        assert resp.status_code == 403, f"{method} {rule.rule} not CSRF-gated ({resp.status_code})"
        checked += 1
    assert checked >= 10  # sanity: we actually exercised the mutating routes


def test_tails_reports_a_persistent_device():
    # Inject a tracker with a device seen across the last few windows (relative to real now, so the
    # endpoint's time.time() query lands in the same span) → it flags as a persistent tail.
    import time as _t

    from src.core.tail_detect import PersistenceTracker

    tracker = PersistenceTracker(window_seconds=300, num_windows=4)
    now = _t.time()
    for i in range(4):  # one sighting in each of the last 4 windows → persistence 1.0
        tracker.observe("ble:e2:14:9c", now - i * 300 - 1, label="AirTag")
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool(),
                           tail_tracker=tracker)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = "tok"
    data = c.get("/api/tails").get_json()
    assert any(h["device"] == "ble:e2:14:9c" and h["persistence"] >= 0.5 for h in data)


def test_tails_empty_by_default_and_requires_auth():
    c = _client(DeviceManager())
    assert c.get("/api/tails").get_json() == []
    c2 = _client(DeviceManager(), authed=False)
    assert c2.get("/api/tails").status_code == 401


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


def test_reform_flasher_grouped_search_and_categories():
    # The flasher picker groups + filters (search box + category dropdown) so ~50 profiles don't
    # render as one long scroll. Presentation only — the flash/variant wiring is untouched.
    body = _client(DeviceManager()).get("/reform").get_data(as_text=True)
    assert 'id="fw-search"' in body
    assert 'id="fw-cat"' in body
    assert "data-cat=" in body and "data-name=" in body
    assert "Wi-Fi / BLE multitools" in body   # a category label from the classifier
    assert "fw-flash" in body and "fw-variant" in body   # flash wiring intact


def test_flash_category_classifier_buckets_are_sane():
    from src.ui.web.app import _flash_category, _flash_rows

    assert _flash_category("ESP32 Marauder") == "Wi-Fi / BLE multitools"
    assert _flash_category("ESP32 Dual-Band Wardriver") == "Wardriving"
    assert _flash_category("BW16 Deauther") == "Offensive / lab-only"
    assert _flash_category("Meshtastic") == "Mesh / LoRa"
    assert _flash_category("Custom / local `.bin`") == "Other"
    # every row gets exactly one category, rows are grouped in category order
    rows, cats = _flash_rows(["ESP32 Marauder", "BW16 Deauther", "Meshtastic"])
    assert {r["cat"] for r in rows} <= set(cats)
    assert len(rows) == 3
