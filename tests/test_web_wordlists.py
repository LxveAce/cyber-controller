"""B3 — the CRACK wordlist-management routes (/api/wordlists[/download|/byo])."""
from __future__ import annotations

import pytest

pytest.importorskip("flask")

from src.core import wordlist_manager as wl
from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.security.web_auth import new_csrf_token
from src.ui.web.app import create_app


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")
    # point the wordlist dir at an empty temp dir so scan_installed starts clean
    monkeypatch.setenv("CC_WORDLIST_DIR", str(tmp_path / "wordlists"))


def _client():
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool())
    c = app.test_client()
    token = new_csrf_token()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = token
    return c, token


def test_wordlists_inventory():
    c, _t = _client()
    r = c.get("/api/wordlists")
    assert r.status_code == 200
    body = r.get_json()
    assert set(body) >= {"bundled", "installed", "catalog", "dir"}
    ids = {e["id"] for e in body["catalog"]}
    assert {"wpa-top62", "rockyou"} <= ids            # the curated catalog is exposed
    assert all("installed" in e and "size_human" in e for e in body["catalog"])
    assert body["installed"] == []                    # fresh temp dir


def test_byo_registers_a_real_file(tmp_path):
    c, token = _client()
    f = tmp_path / "mylist.txt"
    f.write_text("hunter2\npassword1\n", encoding="utf-8")
    r = c.post("/api/wordlists/byo", json={"path": str(f)}, headers={"X-CSRF-Token": token})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_byo_rejects_missing_path_and_bad_file(tmp_path):
    c, token = _client()
    assert c.post("/api/wordlists/byo", json={}, headers={"X-CSRF-Token": token}).status_code == 400
    bad = c.post("/api/wordlists/byo", json={"path": str(tmp_path / "nope.txt")},
                 headers={"X-CSRF-Token": token})
    assert bad.status_code == 400
    assert "ok" in bad.get_json() and bad.get_json()["ok"] is False


def test_download_unknown_id_400():
    c, token = _client()
    r = c.post("/api/wordlists/download", json={"id": "does-not-exist"},
               headers={"X-CSRF-Token": token})
    assert r.status_code == 400
    assert "unknown wordlist id" in r.get_json()["error"]


def test_download_success_is_wired(monkeypatch, tmp_path):
    # Don't hit the network: prove the route calls download_wordlist for a valid id and returns its path.
    called = {}

    def fake_dl(spec, directory=None, on_line=None, **kw):
        called["id"] = spec.id
        if on_line:
            on_line("[download] fake ok")
        return str(tmp_path / "wpa-top62.txt")

    monkeypatch.setattr(wl, "download_wordlist", fake_dl)
    c, token = _client()
    r = c.post("/api/wordlists/download", json={"id": "wpa-top62"}, headers={"X-CSRF-Token": token})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["id"] == "wpa-top62"
    assert called["id"] == "wpa-top62"
    assert any("fake ok" in ln for ln in body["log"])


def test_download_requires_csrf():
    c, _t = _client()
    # No CSRF header -> rejected (state-changing POST)
    r = c.post("/api/wordlists/download", json={"id": "wpa-top62"})
    assert r.status_code in (400, 403)
