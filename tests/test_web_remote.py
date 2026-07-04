"""Web Remote page (MB P2) — the touch-first quick-command home.

Asserts it is auth-gated, renders REAL protocol commands as buttons with danger badges + the disclaimer,
excludes arg commands, reuses the guarded /api/command path (no new send endpoint), and degrades gracefully
for a firmware with no one-tap commands.
"""
from __future__ import annotations

import pytest

pytest.importorskip("flask")

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.models.device import Device
from src.security.web_auth import new_csrf_token
from src.ui.web.app import create_app


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")


def _client(dm, authed=True):
    app, _sio = create_app(dm, FlashEngine(), EventBus(), TargetPool())
    client = app.test_client()
    if authed:
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["csrf"] = new_csrf_token()
    return client


def _dm_with(*devices):
    dm = DeviceManager()
    for d in devices:
        dm.add_device(d)
    return dm


def test_remote_requires_auth():
    dm = _dm_with(Device(port="COM7", name="Marauder", firmware="marauder", connected=True))
    resp = _client(dm, authed=False).get("/remote")
    assert resp.status_code == 401


def test_remote_renders_real_commands_with_badges():
    dm = _dm_with(Device(port="COM7", name="Marauder", firmware="marauder", connected=True))
    body = _client(dm).get("/remote").get_data(as_text=True)
    assert "Authorized use only" in body                      # disclaimer present
    assert 'data-command="scanall"' in body                   # a real safe command button
    assert 'data-command="attack -t deauth"' in body          # a real flagged command button
    assert "badge-lab" in body                                # danger badge rendered
    assert "/api/command" in body and "X-CSRF-Token" in body  # reuses the guarded send path + CSRF
    assert "select -a &lt;idx&gt;" not in body and "<idx>" not in body  # arg commands excluded


def test_remote_no_device_shows_empty_notice():
    body = _client(_dm_with()).get("/remote").get_data(as_text=True)
    assert "No connected device" in body


def test_remote_disconnected_device_is_skipped():
    dm = _dm_with(Device(port="COM7", name="Marauder", firmware="marauder", connected=False))
    body = _client(dm).get("/remote").get_data(as_text=True)
    assert "No connected device" in body                      # not connected -> not shown
    assert 'data-command="scanall"' not in body


def test_remote_renders_ghostesp_real_commands():
    # ghostesp resolves despite the device_detect('ghostesp') vs registry('ghost-esp') mismatch -> real
    # commands render, NOT the empty fallback (regression: this showed the terminal note before the fix).
    dm = _dm_with(Device(port="COM9", name="GhostESP", firmware="ghostesp", connected=True))
    body = _client(dm).get("/remote").get_data(as_text=True)
    assert "No one-tap commands for this firmware" not in body
    assert "data-command=" in body


def test_remote_unknown_firmware_shows_terminal_note():
    dm = _dm_with(Device(port="COM8", name="Mystery", firmware="mystery-fw", connected=True))
    body = _client(dm).get("/remote").get_data(as_text=True)
    assert "No one-tap commands for this firmware" in body    # honest fallback for a firmware with no catalog
    assert "/terminal/COM8" in body


def test_remote_firmware_none_does_not_crash():
    dm = _dm_with(Device(port="COM6", name="Unknown", firmware=None, connected=True))
    body = _client(dm).get("/remote").get_data(as_text=True)  # grouped_quick_commands(None) must not 500
    assert "No one-tap commands for this firmware" in body


def test_remote_nav_link_present():
    body = _client(_dm_with()).get("/remote").get_data(as_text=True)
    assert 'href="/remote"' in body
