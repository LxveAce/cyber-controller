"""N04: a bring-your-own wordlist deleted after it was added must drop out of the picker instead of
staying selectable. The client tracks BYO paths in memory and re-checks them through the endpoint
/api/wordlists/byo/validate; this covers that boundary (picker re-insert is browser-tested)."""

from __future__ import annotations

import pytest

pytest.importorskip("flask")

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.security.web_auth import new_csrf_token
from src.ui.web.app import create_app


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool())
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = new_csrf_token()
        sess["_csrf"] = sess["csrf"]
    return client


def test_deleted_byo_path_drops_out_of_validation(monkeypatch, tmp_path):
    present = tmp_path / "present.txt"
    present.write_text("password123\nhunter2\n", encoding="utf-8")
    missing = tmp_path / "gone.txt"   # never created

    client = _client(monkeypatch, tmp_path)
    with client.session_transaction() as s:
        csrf = s["csrf"]
    resp = client.post("/api/wordlists/byo/validate",
                       json={"paths": [str(present), str(missing)]},
                       headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    valid_paths = [v["requested"] for v in resp.get_json()["valid"]]
    assert str(present) in valid_paths          # the real file survives
    assert str(missing) not in valid_paths      # the deleted file is not presented as available


def test_validate_rejects_non_list_paths(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    with client.session_transaction() as s:
        csrf = s["csrf"]
    resp = client.post("/api/wordlists/byo/validate", json={"paths": "notalist"},
                       headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 400
