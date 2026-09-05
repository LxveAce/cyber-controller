"""B5 — GET /api/captures wires the CRACK table to the shared CaptureStore, display-fields only."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("flask")

from src.core.capture_store import CaptureStore
from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.models.capture import CaptureRecord
from src.security.web_auth import new_csrf_token
from src.ui.web.app import create_app


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")


def _client(store=None):
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool(), capture_store=store)
    c = app.test_client()
    token = new_csrf_token()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["cred_gen"] = c.application.extensions["cc_web_credentials"].generation
        sess["csrf"] = token
    return c


def test_captures_empty_without_store():
    c = _client(None)
    r = c.get("/api/captures")
    assert r.status_code == 200
    assert r.get_json() == {"captures": []}


def test_captures_requires_auth():
    app, _ = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool(), capture_store=CaptureStore())
    r = app.test_client().get("/api/captures")   # no session
    assert r.status_code in (401, 302, 403)


def test_captures_display_fields_only_and_secrets_hidden():
    store = CaptureStore()
    store.add(CaptureRecord(bssid="AA:BB:CC:DD:EE:FF", ssid="HomeNet", capture_type="eapol",
                            device_source="COM4", hc22000_line="WPA*02*deadbeefcafe*...",
                            pcap_path="/sd/secret.pcap", raw="RAW SERIAL LINE"))
    store.add(CaptureRecord(bssid="11:22:33:44:55:66", ssid="CrackedNet", capture_type="pmkid",
                            device_source="COM7", crack_status="cracked", password="hunter2!spring"))
    body = _client(store).get("/api/captures").get_json()
    caps = body["captures"]
    assert len(caps) == 2
    # display fields + the run-wiring fields (key to select a row, crackable flag); still no raw secrets
    allowed = {"ssid", "bssid", "type", "source", "captured", "crack_status", "password", "key", "crackable"}
    for cap in caps:
        assert set(cap) == allowed        # ONLY these fields, no raw pcap/hashline/serial leak
    # crackable/raw material never crosses the wire
    blob = json.dumps(body)
    for secret in ("hc22000", "deadbeef", "/sd/secret.pcap", "RAW SERIAL"):
        assert secret not in blob
    by_type = {cap["type"]: cap for cap in caps}
    assert set(by_type) == {"handshake", "PMKID"}     # eapol -> handshake, pmkid -> PMKID
    assert by_type["PMKID"]["password"] == "hunter2!spring"   # cracked -> shown
    assert by_type["handshake"]["password"] == ""             # uncracked -> blank


def test_captures_password_withheld_until_cracked():
    # A password present on the record but not yet cracked must NOT be exposed.
    store = CaptureStore()
    store.add(CaptureRecord(bssid="99:99:99:99:99:99", ssid="Pending", capture_type="eapol",
                            crack_status="running", password="should-not-appear"))
    caps = _client(store).get("/api/captures").get_json()["captures"]
    assert caps[0]["password"] == ""
    assert "should-not-appear" not in json.dumps(caps)
