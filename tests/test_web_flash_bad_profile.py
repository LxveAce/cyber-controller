"""Web /api/flash — a malformed firmware profile must degrade to a clean 400 JSON error, not an opaque 500.

_load_profiles() registers every *.json under its file stem even when FirmwareProfile.from_file raises, so a
corrupt profile is a valid, selectable profile_id. When the flash actually runs, load_profile re-parses and
raises; the tk and TUI paths already wrap this in try/except and surface a specific message — the web remote
was the inconsistent one, letting the parse error escape as a generic HTTP 500. It must give a 400 too.
"""
from __future__ import annotations

import pytest

pytest.importorskip("flask")

import src.ui.web.app as web_app
from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.models.device import Device
from src.security.web_auth import new_csrf_token
from src.ui.web.app import create_app


def _make_client(monkeypatch, tmp_path, *, profile_body: str):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")

    prof_dir = tmp_path / "profiles"
    prof_dir.mkdir()
    (prof_dir / "corrupt.json").write_text(profile_body, encoding="utf-8")
    # Point the loader at our tmp dir so 'corrupt' becomes a registered, selectable profile_id.
    monkeypatch.setattr(web_app, "_PROFILES_DIR", prof_dir)

    dm = DeviceManager()
    dm.add_device(Device(port="COM7", name="Marauder", firmware="marauder", connected=True))

    app, _sio = create_app(dm, FlashEngine(), EventBus(), TargetPool())
    token = new_csrf_token()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = token
    return client, token


def _post_flash(client, token, profile_id):
    return client.post(
        "/api/flash",
        json={"port": "COM7", "profile_id": profile_id, "_csrf": token},
        headers={"X-CSRF-Token": token},
    )


def test_corrupt_profile_returns_400_not_500(monkeypatch, tmp_path):
    # Syntactically-invalid JSON -> FirmwareProfile.from_file raises on the re-parse in load_profile.
    client, token = _make_client(monkeypatch, tmp_path, profile_body="{ this is not valid json")
    resp = _post_flash(client, token, "corrupt")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "Invalid firmware profile" in data["error"]
    assert "corrupt.json" in data["error"]


def test_unknown_profile_still_404(monkeypatch, tmp_path):
    # Distinguishes the load-failure 400 from the pre-existing unknown-profile 404: an unregistered id 404s,
    # proving the 400 above is genuinely produced by the load_profile guard, not the unknown-profile check.
    client, token = _make_client(monkeypatch, tmp_path, profile_body="{ this is not valid json")
    resp = _post_flash(client, token, "does-not-exist")
    assert resp.status_code == 404
