"""RED-TEAM the endpoints shipped in the 2026-08-08 reform run — adversarial probes for auth/CSRF
bypass, settings key-injection, broadcast offensive-gate evasion, CSV formula-injection in the
captures export, and secret leakage. Red-team-after-each-phase discipline; findings are fixed with a
regression test that stays here."""
from __future__ import annotations

import pytest

pytest.importorskip("flask")

from src.config import settings as app_settings
from src.core.capture_store import CaptureStore
from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.core.sensing_model import SensingModel
from src.models.capture import CaptureRecord
from src.models.device import Device
from src.ui.web.app import create_app


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "pw-123")
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(app_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", settings_file)
    return settings_file


def _client(authed=True, **kw):
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool(), **kw)
    c = app.test_client()
    if authed:
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["csrf"] = "tok"
    return c


# ── auth: every new route rejects the unauthenticated ──
def test_all_new_routes_require_auth():
    c = _client(authed=False)
    assert c.get("/api/settings").status_code == 401
    assert c.get("/api/version").status_code == 401
    assert c.get("/api/sensing").status_code == 401
    assert c.get("/api/captures/export").status_code == 401
    assert c.post("/api/settings").status_code == 401
    assert c.post("/api/updates/check").status_code == 401
    assert c.post("/api/broadcast").status_code == 401


# ── csrf: every new mutating route rejects a missing token ──
def test_new_mutating_routes_require_csrf():
    c = _client()
    assert c.post("/api/settings", json={"serial": {"default_baud": 9600}}).status_code == 403
    assert c.post("/api/updates/check", json={}).status_code == 403
    assert c.post("/api/broadcast", json={"command": "x", "ports": ["COM9"]}).status_code == 403


# ── settings: no key-injection, reset is strict, type-confusion doesn't crash ──
def test_settings_ignores_non_whitelisted_keys(_isolate):
    c = _client()
    r = c.post("/api/settings", json={
        "serial": {"default_baud": 9600},
        "evil": {"x": 1}, "__proto__": {"y": 2}, "safety": {"backdoor": True},
    }, headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 200
    import json
    on_disk = json.loads(_isolate.read_text())
    assert "evil" not in on_disk and "__proto__" not in on_disk
    assert "backdoor" not in on_disk.get("safety", {})
    # the whitelisted change DID persist
    assert on_disk["serial"]["default_baud"] == 9600


def test_settings_reset_requires_exactly_true(_isolate):
    c = _client()
    c.post("/api/settings", json={"serial": {"default_baud": 921600}},
           headers={"X-CSRF-Token": "tok"})
    # a truthy-but-not-True reset value must NOT wipe settings to defaults
    r = c.post("/api/settings", json={"reset": "yes"}, headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 200 and r.get_json().get("reset") is not True
    import json
    assert json.loads(_isolate.read_text())["serial"]["default_baud"] == 921600


def test_settings_type_confusion_does_not_crash(_isolate):
    c = _client()
    for body in ({"serial": "notadict"}, {"flash": ["a"]}, {"uploads": 5}, [1, 2, 3], "string", 42):
        r = c.post("/api/settings", json=body, headers={"X-CSRF-Token": "tok"})
        assert r.status_code in (200, 400)   # never a 500


def test_settings_never_returns_the_raw_wigle_token(_isolate):
    c = _client()
    secret = "S3cr3t-Wigle-Token=="
    c.post("/api/settings", json={"uploads": {"wigle_token": secret}},
           headers={"X-CSRF-Token": "tok"})
    assert secret not in c.get("/api/settings").get_data(as_text=True)


# ── broadcast: the offensive gate resists case/whitespace evasion ──
def _bc_client():
    dm = DeviceManager()

    class _Conn:
        is_connected = True

        def __init__(self):
            self.writes = []

        def write(self, s):
            self.writes.append(s)

    dm.add_device(Device(port="COM9", name="M", firmware="marauder", connected=True))
    conn = _Conn()
    dm._connections["COM9"] = conn
    app, _sio = create_app(dm, FlashEngine(), EventBus(), TargetPool())
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = "tok"
    return c, conn


def test_broadcast_gate_resists_case_and_whitespace_evasion():
    c, conn = _bc_client()
    # genuinely-offensive (transmitting) verbs — incl. metadata-danger ones classify() misses —
    # must be gated even with case/whitespace mangling.
    for evasion in ("DEAUTH", "  deauth", "Attack -t deauth", "  EVILPORTAL", "startportal"):
        r = c.post("/api/broadcast", json={"command": evasion, "ports": ["COM9"]},
                   headers={"X-CSRF-Token": "tok"})
        assert r.status_code == 403, f"offensive verb slipped the gate un-consented: {evasion!r}"
    assert conn.writes == []   # nothing transmitted without consent

    # positive control: a PASSIVE recon verb (sniffdeauth = listen for deauth, no TX) must NOT
    # be gated — it fans out freely, so the gate isn't over-blocking recon.
    r = c.post("/api/broadcast", json={"command": "sniffdeauth", "ports": ["COM9"]},
               headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 200 and conn.writes == ["sniffdeauth"]


def test_broadcast_rejects_oversized_and_malformed_ports():
    c, _ = _bc_client()
    assert c.post("/api/broadcast", json={"command": "scanall", "ports": ["COM9"] * 65},
                  headers={"X-CSRF-Token": "tok"}).status_code == 400
    assert c.post("/api/broadcast", json={"command": "scanall", "ports": "COM9"},
                  headers={"X-CSRF-Token": "tok"}).status_code == 400


# ── captures export: CSV formula-injection is neutralized, secrets absent ──
def test_captures_export_neutralizes_csv_formula_injection():
    store = CaptureStore()
    # a hostile SSID (attacker controls the beacon) that would run in Excel if written raw
    store.add(CaptureRecord(bssid="AA:BB:CC:DD:EE:FF", capture_type="pmkid",
                            ssid="=HYPERLINK(\"http://evil\")", device_source="COM9"))
    body = _client(capture_store=store).get("/api/captures/export").get_data(as_text=True)
    # the dangerous cell must NOT start a formula (leading =,+,-,@ neutralized)
    for line in body.splitlines()[1:]:
        cell = line.split(",")[0].strip().strip('"')
        assert cell[:1] not in ("=", "+", "-", "@"), f"formula-injectable cell: {cell!r}"


def test_captures_export_never_leaks_password():
    store = CaptureStore()
    store.add(CaptureRecord(bssid="AA:BB:CC:DD:EE:FF", capture_type="eapol", ssid="Net"))
    store.mark_cracked(store.all()[0].key, "leaked-pw-should-not-appear")
    body = _client(capture_store=store).get("/api/captures/export").get_data(as_text=True)
    assert "leaked-pw-should-not-appear" not in body


# ── sensing: read-only, no secret/target shape, graceful without a model ──
def test_sensing_is_readonly_and_leak_free():
    m = SensingModel()
    m.observe({"presence": True, "motion": 0.5, "confidence": 0.7, "node_id": "n1"}, now=1.0)
    c = _client(sensing_model=m)
    # POST is not a method on this route
    assert c.post("/api/sensing", headers={"X-CSRF-Token": "tok"}).status_code == 405
    body = c.get("/api/sensing").get_data(as_text=True)
    assert "mac" not in body and "password" not in body
