"""CRACK ▸ Captured Handshakes CSV export (/api/captures/export). Guards: auth-gated, correct CSV
shape, and the load-bearing invariant — a recovered password (or pcap path / hashline) is NEVER
written into the portable export file, even for a cracked capture (stricter than /api/captures,
which shows a cracked password on the loopback screen only)."""
from __future__ import annotations

import pytest

pytest.importorskip("flask")

from src.core.capture_store import CaptureStore
from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.models.capture import CaptureRecord
from src.ui.web.app import create_app


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")


def _client(store=None, authed=True):
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool(),
                           capture_store=store)
    c = app.test_client()
    if authed:
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["cred_gen"] = c.application.extensions["cc_web_credentials"].generation
            sess["csrf"] = "tok"
    return c


def test_export_requires_auth():
    assert _client(authed=False).get("/api/captures/export").status_code == 401


def test_export_empty_store_is_header_only_csv():
    r = _client(CaptureStore()).get("/api/captures/export")
    assert r.status_code == 200
    assert r.mimetype == "text/csv"
    assert "attachment" in r.headers.get("Content-Disposition", "")
    body = r.get_data(as_text=True)
    assert body.splitlines()[0] == "ssid,bssid,type,source,captured,crack_status"


def test_export_lists_captures_with_display_fields():
    store = CaptureStore()
    store.add(CaptureRecord(bssid="AA:BB:CC:DD:EE:FF", capture_type="pmkid", ssid="CoffeeShop",
                            device_source="COM9"))
    body = _client(store).get("/api/captures/export").get_data(as_text=True)
    assert "CoffeeShop" in body and "AA:BB:CC:DD:EE:FF" in body
    assert "PMKID" in body and "COM9" in body


def test_export_never_leaks_a_recovered_password_or_pcap_path():
    store = CaptureStore()
    store.add(CaptureRecord(bssid="AA:BB:CC:DD:EE:FF", capture_type="eapol", ssid="MyNet",
                            device_source="COM9", pcap_path="/secret/on-disk/handshake.pcap"))
    key = store.all()[0].key
    secret = "hunter2-super-secret"
    assert store.mark_cracked(key, secret, detail="found") is True
    r = _client(store).get("/api/captures/export")
    body = r.get_data(as_text=True)
    # the row is present + marked cracked...
    assert "MyNet" in body and "cracked" in body
    # ...but the portable file carries NO secret: not the password, not the pcap path
    assert secret not in body
    assert "handshake.pcap" not in body and "/secret/" not in body
    # and there is no password column at all
    assert "password" not in body.splitlines()[0].lower()


def test_export_handles_missing_store_gracefully():
    # create_app with no capture_store must still serve an (empty) CSV, not 500.
    r = _client(None).get("/api/captures/export")
    assert r.status_code == 200
    header = "ssid,bssid,type,source,captured,crack_status"
    assert r.get_data(as_text=True).splitlines()[0] == header
