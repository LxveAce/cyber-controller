"""Reform SETTINGS surface (B17) — live read + owner write-back to the real settings store.

The reform SETTINGS view reads and writes ~/.cyber-controller/settings.json through the SAME
src.config.settings store the desktop app uses. These tests pin: the GET/POST round-trip actually
persists; validation rejects junk without saving; reset restores defaults; the version + Updates
plumbing works; and — the load-bearing invariant — a secret (the WiGLE token) NEVER crosses the
wire in the clear (GET exposes only a `wigle_token_set` boolean, and a write leaves the stored
token alone unless the client sends a real new value).
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("flask")

from src.config import settings as app_settings
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
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(app_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", settings_file)
    return settings_file


def _client(authed=True):
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool())
    c = app.test_client()
    if authed:
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["csrf"] = "tok"
    return c


# ── auth / csrf gating ───────────────────────────────────────────────

def test_settings_get_requires_auth():
    assert _client(authed=False).get("/api/settings").status_code == 401


def test_settings_post_requires_auth():
    assert _client(authed=False).post("/api/settings").status_code == 401


def test_settings_post_requires_csrf():
    # authed but no X-CSRF-Token → 403
    r = _client().post("/api/settings", json={"serial": {"default_baud": 9600}})
    assert r.status_code == 403


# ── read ─────────────────────────────────────────────────────────────

def test_settings_get_is_secret_free_and_shaped():
    r = _client().get("/api/settings")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    s = body["settings"]
    # the surfaces the reform view renders
    assert s["serial"]["default_baud"] == 115200
    assert set(s["flash"]) == {"flash_baud", "verify", "auto_backup", "mode"}
    assert s["interface"]["touch_mode"] == "auto"
    # the WiGLE secret is exposed ONLY as a boolean flag, never as a raw `wigle_token` value
    assert s["uploads"] == {"wigle_token_set": False}
    assert "wigle_token" not in s["uploads"]


# ── write-back round-trip ────────────────────────────────────────────

def test_flash_baud_auto_persists_as_none(_isolate):
    # F03 precedence: the "Auto" choice (client sends null or "auto") persists as None so the firmware's
    # own baud is used — it does NOT silently round-trip to the 921600 default.
    c = _client()
    assert c.post("/api/settings", json={"flash": {"flash_baud": None}},
                  headers={"X-CSRF-Token": "tok"}).status_code == 200
    assert c.get("/api/settings").get_json()["settings"]["flash"]["flash_baud"] is None
    on_disk = json.loads(_isolate.read_text(encoding="utf-8"))
    assert on_disk["flash"]["flash_baud"] is None
    # the "auto" string form is accepted the same way
    assert c.post("/api/settings", json={"flash": {"flash_baud": "auto"}},
                  headers={"X-CSRF-Token": "tok"}).status_code == 200
    assert c.get("/api/settings").get_json()["settings"]["flash"]["flash_baud"] is None


def test_flash_baud_default_is_auto_none(_isolate):
    # A fresh install defaults to Auto (None), not a hardcoded 921600 that would override every profile.
    assert _client().get("/api/settings").get_json()["settings"]["flash"]["flash_baud"] is None


def test_settings_post_persists(_isolate):
    c = _client()
    r = c.post("/api/settings", json={
        "serial": {"default_baud": 921600},
        "flash": {"flash_baud": 460800, "verify": False, "auto_backup": True, "mode": "qio"},
        "interface": {"touch_mode": "on"},
        "safety": {"confirm_dangerous": True, "suppress_all_warnings": True},
    }, headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 200
    # the response reflects the change
    s = r.get_json()["settings"]
    assert s["serial"]["default_baud"] == 921600
    assert s["flash"]["mode"] == "qio"
    assert s["interface"]["touch_mode"] == "on"
    # and it truly hit disk via the real store (not just the response)
    on_disk = json.loads(_isolate.read_text(encoding="utf-8"))
    assert on_disk["serial"]["default_baud"] == 921600
    assert on_disk["safety"]["suppress_all_warnings"] is True
    # a fresh GET re-reads the persisted value
    assert c.get("/api/settings").get_json()["settings"]["flash"]["flash_baud"] == 460800


def test_settings_post_rejects_bad_value_and_saves_nothing(_isolate):
    c = _client()
    r = c.post("/api/settings", json={"serial": {"default_baud": 12345}},
               headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 400
    assert "serial.default_baud" in r.get_json()["errors"]
    # nothing was written — the file must not exist (or must still hold the default)
    assert not _isolate.exists() or \
        json.loads(_isolate.read_text())["serial"]["default_baud"] == 115200


def test_settings_post_rejects_bad_touch_mode():
    r = _client().post("/api/settings", json={"interface": {"touch_mode": "sideways"}},
                       headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 400
    assert "interface.touch_mode" in r.get_json()["errors"]


def test_settings_reset_restores_defaults(_isolate):
    c = _client()
    c.post("/api/settings", json={"serial": {"default_baud": 921600}},
           headers={"X-CSRF-Token": "tok"})
    r = c.post("/api/settings", json={"reset": True}, headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 200
    assert r.get_json()["reset"] is True
    assert r.get_json()["settings"]["serial"]["default_baud"] == 115200


# ── the WiGLE secret never leaks, and is only changed on a real new value ──

def test_wigle_token_sets_flag_but_value_never_returned(_isolate):
    c = _client()
    secret = "ZmFrZS10b2tlbi1kby1ub3Qtc2hhcmU="
    r = c.post("/api/settings", json={"uploads": {"wigle_token": secret}},
               headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 200
    assert r.get_json()["settings"]["uploads"] == {"wigle_token_set": True}
    # the value is stored on disk...
    assert json.loads(_isolate.read_text())["uploads"]["wigle_token"] == secret
    # ...but NEVER echoed back through the API
    assert secret not in c.get("/api/settings").get_data(as_text=True)


def test_wigle_masked_placeholder_leaves_token_unchanged(_isolate):
    c = _client()
    secret = "real-token-value"
    c.post("/api/settings", json={"uploads": {"wigle_token": secret}},
           headers={"X-CSRF-Token": "tok"})
    # a later save that sends only the bullet placeholder must NOT wipe the token
    c.post("/api/settings", json={"uploads": {"wigle_token": "••••••••"}},
           headers={"X-CSRF-Token": "tok"})
    assert json.loads(_isolate.read_text())["uploads"]["wigle_token"] == secret


def test_wigle_blank_string_clears_token(_isolate):
    c = _client()
    c.post("/api/settings", json={"uploads": {"wigle_token": "something"}},
           headers={"X-CSRF-Token": "tok"})
    c.post("/api/settings", json={"uploads": {"wigle_token": ""}},
           headers={"X-CSRF-Token": "tok"})
    assert json.loads(_isolate.read_text())["uploads"]["wigle_token"] == ""


# ── version / updates plumbing ───────────────────────────────────────

def test_version_endpoint_reports_running_build():
    from src.version import __version__
    r = _client().get("/api/version")
    assert r.status_code == 200
    assert r.get_json()["version"] == __version__


def test_updates_check_requires_csrf():
    assert _client().post("/api/updates/check").status_code == 403
