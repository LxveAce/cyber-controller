"""The reform MAP▸Wardrive "upload a saved WiGLE CSV" route (/api/wardrive/upload).

Uploading an already-exported CSV is device-independent (unlike a live survey), so this path is
wired even though the web survey backend isn't. These tests pin the honest gates: auth + csrf are
required, the WiGLE token must be set in Settings first, the path must point at a real file, and a
success/failure from the upload core is surfaced without ever echoing the token or the file bytes.
The one network call (src.core.wardrive_upload.upload_csv) is monkeypatched — no traffic leaves.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("flask")

from src.config import settings as app_settings
from src.core import wardrive_upload
from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.ui.web.app import create_app


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    # Never touch the real machine's gate or settings file.
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")
    monkeypatch.setattr(app_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", tmp_path / "settings.json")


def _client(authed=True):
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool())
    c = app.test_client()
    if authed:
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["csrf"] = "tok"
    return c


def _set_token(value="dGVzdDp0b2tlbg=="):
    s = app_settings.load_settings()
    s["uploads"]["wigle_token"] = value
    app_settings.save_settings(s)


def _csv(tmp_path):
    f = tmp_path / "wardrive.csv"
    f.write_text("WigleWifi-1.6\nMAC,SSID\nAA:BB:CC:DD:EE:FF,home\n", encoding="utf-8")
    return f


# ── gating ───────────────────────────────────────────────────────────

def test_upload_requires_auth():
    assert _client(authed=False).post("/api/wardrive/upload").status_code == 401


def test_upload_requires_csrf():
    r = _client().post("/api/wardrive/upload", json={"path": "x"})
    assert r.status_code == 403


def test_upload_requires_a_token(tmp_path):
    # A real CSV but no WiGLE token set → refused with a plain, actionable message (no upload attempted).
    r = _client().post("/api/wardrive/upload", json={"path": str(_csv(tmp_path))},
                       headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 400
    assert "token" in r.get_json()["error"].lower()


def test_upload_requires_a_real_path():
    _set_token()
    assert _client().post("/api/wardrive/upload", json={}, headers={"X-CSRF-Token": "tok"}).status_code == 400
    r = _client().post("/api/wardrive/upload", json={"path": "/no/such/file.csv"},
                       headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 400


# ── happy path + failure surfacing (upload_csv monkeypatched — no network) ──

def test_upload_success_returns_transid(tmp_path, monkeypatch):
    _set_token()
    seen = {}

    def _fake_upload(path, token, *, provider="wigle", donate=False, **_):
        seen.update(path=path, token=token, provider=provider, donate=donate)
        return {"transid": "20260812-00042", "message": "uploaded"}

    monkeypatch.setattr(wardrive_upload, "upload_csv", _fake_upload)
    r = _client().post("/api/wardrive/upload", json={"path": str(_csv(tmp_path)), "donate": True},
                       headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["transid"] == "20260812-00042"
    assert seen["provider"] == "wigle" and seen["donate"] is True     # token used, not echoed
    assert "token" not in body and "dGVzdDp0b2tlbg==" not in json.dumps(body)


def test_upload_error_is_surfaced_not_raised(tmp_path, monkeypatch):
    _set_token()

    def _boom(*_a, **_k):
        raise wardrive_upload.UploadError("WiGLE rejected the token (401) — check the token in Settings")

    monkeypatch.setattr(wardrive_upload, "upload_csv", _boom)
    r = _client().post("/api/wardrive/upload", json={"path": str(_csv(tmp_path))},
                       headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 502
    assert r.get_json()["ok"] is False
    assert "WiGLE" in r.get_json()["error"]
