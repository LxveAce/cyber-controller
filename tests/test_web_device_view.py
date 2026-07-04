"""Web Device View (MB P3) — /device/<port> renders the firmware's reconstructed menu, auth-gated, wired to
the guarded /api/command, degrading gracefully for a firmware with no skin."""
from __future__ import annotations

import json
import re

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
    c = app.test_client()
    if authed:
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["csrf"] = new_csrf_token()
    return c


def _dm_with(*devices):
    dm = DeviceManager()
    for d in devices:
        dm.add_device(d)
    return dm


def test_device_view_requires_auth():
    dm = _dm_with(Device(port="COM7", name="Marauder", firmware="marauder", connected=True))
    assert _client(dm, authed=False).get("/device/COM7").status_code == 401


def test_device_view_renders_menu_tree():
    dm = _dm_with(Device(port="COM7", name="Marauder", firmware="marauder", connected=True))
    body = _client(dm).get("/device/COM7").get_data(as_text=True)
    assert "Device View" in body and "Authorized use only" in body
    assert "/api/command" in body and "X-CSRF-Token" in body   # reuses the guarded send path
    # the embedded JSON tree is present, valid, and carries real menu data + danger tags
    m = re.search(r'id="dv-tree"[^>]*>(.*?)</script>', body, re.S)
    assert m, "embedded menu tree not found"
    tree = json.loads(m.group(1).replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&"))
    assert tree["firmware"] == "marauder" and tree["root"]
    labels = json.dumps(tree)
    assert "attack -t deauth" in labels and "lab-only" in labels    # a flagged leaf survived serialization


def test_device_view_ghostesp_resolves_despite_naming():
    dm = _dm_with(Device(port="COM9", name="GhostESP", firmware="ghostesp", connected=True))
    body = _client(dm).get("/device/COM9").get_data(as_text=True)
    assert "No reconstructed on-screen menu" not in body
    assert '"firmware": "ghostesp"' in body


def test_device_view_no_skin_firmware_degrades():
    dm = _dm_with(Device(port="COM8", name="Flipper", firmware="flipper", connected=True))
    body = _client(dm).get("/device/COM8").get_data(as_text=True)
    assert "No reconstructed on-screen menu" in body
    assert "/terminal/COM8" in body


def test_device_view_unknown_port_degrades_no_crash():
    body = _client(_dm_with()).get("/device/COM404").get_data(as_text=True)  # no such device
    assert "No reconstructed on-screen menu" in body


def test_device_view_json_has_no_script_break():
    # the embedded tree must not contain a literal </script> (would break out of the script tag)
    dm = _dm_with(Device(port="COM7", name="Marauder", firmware="marauder", connected=True))
    body = _client(dm).get("/device/COM7").get_data(as_text=True)
    tree_block = re.search(r'id="dv-tree"[^>]*>(.*?)</script>', body, re.S).group(1)
    assert "</script" not in tree_block.lower()


def test_device_view_escapes_hostile_label(monkeypatch):
    """The XSS defense must neutralize a HOSTILE label — not just pass on the clean developer tree.
    Inject a </script><img onerror> label via menu_tree and assert it cannot break out + round-trips."""
    import src.core.device_menus as dm
    hostile = {"firmware": "marauder", "title": "X", "root": [
        {"label": "</script><img src=x onerror=alert(1)>&pwn", "command": "info", "needs_arg": False, "danger": ""}]}
    monkeypatch.setattr(dm, "menu_tree", lambda _fw: hostile)  # route imports menu_tree at call time
    dmgr = _dm_with(Device(port="COM7", name="M", firmware="marauder", connected=True))
    body = _client(dmgr).get("/device/COM7").get_data(as_text=True)
    block = re.search(r'id="dv-tree"[^>]*>(.*?)</script>', body, re.S).group(1)
    assert "</script" not in block.lower() and "<img" not in block.lower()   # neutralized
    restored = json.loads(block.replace("\u003c", "<").replace("\u003e", ">").replace("\u0026", "&"))
    assert restored["root"][0]["label"] == "</script><img src=x onerror=alert(1)>&pwn"   # round-trips intact


def test_device_view_shows_disconnected_indicator():
    dm = _dm_with(Device(port="COM7", name="Marauder", firmware="marauder", connected=False))
    body = _client(dm).get("/device/COM7").get_data(as_text=True)
    assert "disconnected" in body and "preview only" in body   # menu still renders, honestly labelled
