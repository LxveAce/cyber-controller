"""Audit L-4: the web CSP must use a per-request nonce for script-src (no 'unsafe-inline'),
and every inline <script> the templates emit must carry that nonce."""
from __future__ import annotations

import base64
import re


def _make_client(monkeypatch, tmp_path):
    # Isolate the three host persistence paths this test's app construction would otherwise read or
    # write, BEFORE importing and constructing the app. All three resolve under ~/.cyber-controller: the
    # physical-key gate (its lockout counters — a failed/None-verifying login rewrites them, so a run can
    # lock the real machine gate), the saved web password (which overrides CC_WEB_PASS), and the persisted
    # Flask session key. physical_key._config_path() honours CC_GATE_CONFIG; the rest resolve module-level
    # constants at call time, so setattr redirects them. Local to this test — no runtime or conftest change.
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "access_gate.json"))
    import src.security.physical_key as physical_key
    import src.security.web_auth as web_auth
    monkeypatch.setattr(physical_key, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(web_auth, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(web_auth, "_WEB_AUTH_FILE", tmp_path / "web_auth.json")
    monkeypatch.setattr(web_auth, "_SECRET_KEY_FILE", tmp_path / "web_secret.key")

    # Heavyweight imports AFTER isolation so no dependency or app factory runs before the seams redirect.
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.web.app import create_app

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


def test_csp_script_src_uses_nonce_not_unsafe_inline(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path)
    # after_request attaches the CSP even on the 401 (no-auth) path.
    resp = client.get("/")
    ss = _script_src(resp.headers.get("Content-Security-Policy", ""))
    assert "'nonce-" in ss
    assert "'unsafe-inline'" not in ss  # the whole point of L-4


def test_csp_nonce_is_per_request(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path)
    a = _script_src(client.get("/").headers["Content-Security-Policy"])
    b = _script_src(client.get("/").headers["Content-Security-Policy"])
    assert a and b and a != b  # a fresh nonce each request


def test_rendered_scripts_carry_the_matching_nonce(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path)
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
