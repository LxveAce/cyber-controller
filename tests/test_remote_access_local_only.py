"""AUD-1: GET /api/remote-access must disclose a local-only (loopback) bind via ``local_only`` so the UI
doesn't advertise a LAN address as reachable from another device when nothing is listening on it. Isolated
gate config so the machine gate is never touched.
"""
from __future__ import annotations

import pytest

pytest.importorskip("flask")

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.security.web_auth import new_csrf_token
from src.ui.web.app import create_app


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")


def _authed(loopback):
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool(),
                           host_shell_loopback=loopback)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["cred_gen"] = app.extensions["cc_web_credentials"].generation
        sess["csrf"] = new_csrf_token()
    return c


def test_remote_access_reports_local_only_on_loopback_bind():
    r = _authed(loopback=True).get("/api/remote-access").get_json()
    assert r["local_only"] is True     # desktop default: server is bound local-only, LAN URL is not reachable


def test_remote_access_reports_not_local_only_on_lan_bind():
    r = _authed(loopback=False).get("/api/remote-access").get_json()
    assert r["local_only"] is False    # LAN-exposed bind: the LAN address may be used (still not proof)


def test_remote_access_credential_fields_are_unchanged():
    r = _authed(loopback=True).get("/api/remote-access").get_json()
    for key in ("username", "password", "generated", "revealed", "source", "lan_ip", "port", "local_only"):
        assert key in r
