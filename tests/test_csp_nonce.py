"""Audit L-4: the web CSP must use a per-request nonce for script-src (no 'unsafe-inline'),
and every inline <script> the templates emit must carry that nonce."""
from __future__ import annotations

import base64
import re

import pytest

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.ui.web.app import create_app


def _make_client(monkeypatch):
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")
    dm = DeviceManager()
    fe = FlashEngine()
    bus = EventBus()
    pool = TargetPool(bus)
    app, _sio = create_app(dm, fe, bus, pool)
    app.config.update(TESTING=True)
    return app.test_client()


def _script_src(csp: str) -> str:
    for part in csp.split(";"):
        part = part.strip()
        if part.startswith("script-src"):
            return part
    return ""


def test_csp_script_src_uses_nonce_not_unsafe_inline(monkeypatch):
    client = _make_client(monkeypatch)
    # after_request attaches the CSP even on the 401 (no-auth) path.
    resp = client.get("/")
    ss = _script_src(resp.headers.get("Content-Security-Policy", ""))
    assert "'nonce-" in ss
    assert "'unsafe-inline'" not in ss  # the whole point of L-4


def test_csp_nonce_is_per_request(monkeypatch):
    client = _make_client(monkeypatch)
    a = _script_src(client.get("/").headers["Content-Security-Policy"])
    b = _script_src(client.get("/").headers["Content-Security-Policy"])
    assert a and b and a != b  # a fresh nonce each request


def test_rendered_scripts_carry_the_matching_nonce(monkeypatch):
    client = _make_client(monkeypatch)
    auth = base64.b64encode(b"admin:test-pass-123").decode()
    resp = client.get("/", headers={"Authorization": "Basic " + auth})
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    m = re.search(r"'nonce-([A-Za-z0-9_-]+)'", resp.headers["Content-Security-Policy"])
    assert m, "CSP header should carry a nonce"
    nonce = m.group(1)
    scripts = re.findall(r"<script\b[^>]*>", html)
    assert scripts, "dashboard should render at least one <script>"
    for tag in scripts:
        assert f'nonce="{nonce}"' in tag, f"un-nonced script tag would be blocked: {tag}"
