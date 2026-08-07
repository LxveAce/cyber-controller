"""C2 — the host-shell SocketIO bridge in the web app.

Verifies the envelope end-to-end at the web layer: the /api/host-shell probe reports the right state, the
host_shell_* socket handlers do NOT exist unless enabled (a disabled/LAN bind can't be coaxed into a shell),
and when enabled+loopback a real open→echo→output round-trip works.
"""
from __future__ import annotations

import time

import pytest

pytest.importorskip("flask")

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
    # start each test from a known host-shell env
    monkeypatch.delenv("CC_WEB_HOST_SHELL", raising=False)
    monkeypatch.delenv("CC_WEB_ALLOW_LAN", raising=False)


def _make(loopback: bool):
    return create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool(),
                      host_shell_loopback=loopback)


def _http(app):
    c = app.test_client()
    token = new_csrf_token()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = token
    return c, token


def _sio(app, socketio, token, http_client):
    sio = socketio.test_client(app, flask_test_client=http_client, auth={"csrf": token})
    assert sio.is_connected()
    return sio


# ── the /api/host-shell probe ────────────────────────────────────────────────
def test_probe_disabled_by_default():
    app, _ = _make(loopback=True)          # loopback but no opt-in
    c, _tok = _http(app)
    r = c.get("/api/host-shell")
    assert r.status_code == 200
    body = r.get_json()
    assert body["enabled"] is False
    assert "CC_WEB_HOST_SHELL" in body["reason"]


def test_probe_enabled_when_loopback_and_opted_in(monkeypatch):
    monkeypatch.setenv("CC_WEB_HOST_SHELL", "1")
    app, _ = _make(loopback=True)
    c, _tok = _http(app)
    body = c.get("/api/host-shell").get_json()
    assert body["enabled"] is True


def test_probe_refused_on_lan_even_when_opted_in(monkeypatch):
    monkeypatch.setenv("CC_WEB_HOST_SHELL", "1")
    monkeypatch.setenv("CC_WEB_ALLOW_LAN", "1")
    app, _ = _make(loopback=False)
    c, _tok = _http(app)
    body = c.get("/api/host-shell").get_json()
    assert body["enabled"] is False
    assert "LAN" in body["reason"]


# ── socket handlers: absent unless enabled ───────────────────────────────────
def test_handlers_absent_when_disabled():
    app, socketio = _make(loopback=True)   # not opted in -> handlers never defined
    c, token = _http(app)
    sio = _sio(app, socketio, token, c)
    sio.emit("host_shell_open")            # nothing is listening
    time.sleep(0.5)
    received = sio.get_received()
    kinds = {ev["name"] for ev in received}
    assert "host_shell_status" not in kinds
    assert "host_shell_output" not in kinds
    sio.disconnect()


# ── socket handlers: real round-trip when enabled ────────────────────────────
def test_open_echo_roundtrip_when_enabled(monkeypatch):
    monkeypatch.setenv("CC_WEB_HOST_SHELL", "1")
    app, socketio = _make(loopback=True)
    c, token = _http(app)
    sio = _sio(app, socketio, token, c)

    sio.emit("host_shell_open")
    # collect events for a few seconds while the shell spawns + echoes
    tok = "CC_C2_ECHO_5B9D"
    seen_open = False
    output = []
    sio.emit("host_shell_input", {"data": f"echo {tok}\n"})
    deadline = time.time() + 12
    while time.time() < deadline:
        for ev in sio.get_received():
            if ev["name"] == "host_shell_status" and ev["args"] and ev["args"][0].get("open"):
                seen_open = True
            if ev["name"] == "host_shell_output" and ev["args"]:
                output.append(ev["args"][0].get("text", ""))
        if tok in "".join(output):
            break
        time.sleep(0.2)

    joined = "".join(output)
    assert seen_open, "never got host_shell_status open"
    assert tok in joined, f"echo token not seen; output={joined!r}"
    sio.emit("host_shell_close")
    sio.disconnect()


def test_input_before_open_is_reported(monkeypatch):
    monkeypatch.setenv("CC_WEB_HOST_SHELL", "1")
    app, socketio = _make(loopback=True)
    c, token = _http(app)
    sio = _sio(app, socketio, token, c)
    sio.emit("host_shell_input", {"data": "echo nope\n"})
    time.sleep(0.4)
    texts = "".join(ev["args"][0].get("text", "")
                    for ev in sio.get_received() if ev["name"] == "host_shell_output" and ev["args"])
    assert "not open" in texts
    sio.disconnect()
