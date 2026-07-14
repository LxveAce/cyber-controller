"""Beat 271 - session cookie omits Secure behind a reverse-proxy TLS deploy (cc-deep-audit-11 [6]).

`SESSION_COOKIE_SECURE` was tied to LOCAL cert presence only:
`tls_enabled = bool(CC_WEB_CERT and CC_WEB_KEY)` -> `SESSION_COOKIE_SECURE = tls_enabled`. Behind a
TLS-terminating reverse proxy (the documented deployment) the app speaks plaintext HTTP locally, so
CC_WEB_CERT/CC_WEB_KEY are unset, `tls_enabled` is False, and the session cookie is emitted WITHOUT
the Secure attribute -- so it can ride a downgraded/plaintext hop and be captured. Flask decides the
Secure attribute purely from `app.config["SESSION_COOKIE_SECURE"]` (it does NOT consult
request.is_secure / ProxyFix), so the only effective fix is to set that config True for the
upstream-TLS case.

Fix: a tri-state `CC_WEB_COOKIE_SECURE` env override (matching the codebase's `== "1"` convention):
`=1` forces Secure on (upstream TLS), `=0` forces it off (bare-HTTP LAN/testing), unset falls back
to the local-TLS auto-detect. It is an operator-set env, never a client-forgeable header, so it
cannot be used to spoof a downgrade.

Discriminating (fail on buggy HEAD, pass on the fix):
  - test_cookie_secure_forced_on_by_env_override: CC_WEB_COOKIE_SECURE=1, no local cert -> config
    True (HEAD ignores the env: tls_enabled is False -> config False).
  - test_cookie_secure_emitted_in_set_cookie_header: behavior-level -- a real login under the
    override emits a session Set-Cookie carrying `Secure` (HEAD emits it without Secure).
  - test_cookie_secure_explicit_off_overrides_local_tls: local cert present but
    CC_WEB_COOKIE_SECURE=0 -> config False (HEAD ignores the env: tls_enabled True -> config True).
Guards (pass on both HEAD and the fix):
  - test_cookie_secure_default_local_only_unchanged: unset + no cert -> False (default unchanged).
  - test_cookie_secure_auto_on_with_local_tls_unchanged: unset + local cert -> True (auto-detect).
"""
from __future__ import annotations

import base64

import pytest

pytest.importorskip("flask")

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.ui.web.app import create_app


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    # Isolate the access gate to a temp file so a test never locks/rate-limits the real gate.
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")
    # Start each test from a known-clean TLS/override state regardless of the ambient environment.
    for var in ("CC_WEB_CERT", "CC_WEB_KEY", "CC_WEB_COOKIE_SECURE"):
        monkeypatch.delenv(var, raising=False)


def _make_app():
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool())
    return app


def test_cookie_secure_forced_on_by_env_override(monkeypatch):
    """CC_WEB_COOKIE_SECURE=1 must force Secure on even with no local cert (upstream-TLS deploy)."""
    monkeypatch.setenv("CC_WEB_COOKIE_SECURE", "1")
    app = _make_app()
    assert app.config["SESSION_COOKIE_SECURE"] is True, (
        "behind a TLS-terminating proxy the override must mark the session cookie Secure"
    )


def test_cookie_secure_emitted_in_set_cookie_header(monkeypatch):
    """Behavior-level: a real login under the override emits a session cookie carrying Secure."""
    monkeypatch.setenv("CC_WEB_COOKIE_SECURE", "1")
    client = _make_app().test_client()
    cred = base64.b64encode(b"admin:test-pass-123").decode()
    resp = client.get("/", headers={"Authorization": f"Basic {cred}"})
    set_cookie = "; ".join(resp.headers.getlist("Set-Cookie"))
    assert "session=" in set_cookie, "a successful login must set the session cookie"
    assert "Secure" in set_cookie, "the emitted session cookie must carry Secure under upstream TLS"


def test_cookie_secure_explicit_off_overrides_local_tls(monkeypatch):
    """CC_WEB_COOKIE_SECURE=0 must force Secure off even when local TLS is configured."""
    monkeypatch.setenv("CC_WEB_CERT", "/tmp/cc-cert.pem")
    monkeypatch.setenv("CC_WEB_KEY", "/tmp/cc-key.pem")
    monkeypatch.setenv("CC_WEB_COOKIE_SECURE", "0")
    app = _make_app()
    assert app.config["SESSION_COOKIE_SECURE"] is False, (
        "an explicit =0 must win over the local-TLS auto-detect"
    )


def test_cookie_secure_default_local_only_unchanged():
    """Guard: with the override unset and no local cert, Secure stays off (default unchanged)."""
    app = _make_app()
    assert app.config["SESSION_COOKIE_SECURE"] is False


def test_cookie_secure_auto_on_with_local_tls_unchanged(monkeypatch):
    """Guard: with the override unset and a local cert, the auto-detect still enables Secure."""
    monkeypatch.setenv("CC_WEB_CERT", "/tmp/cc-cert.pem")
    monkeypatch.setenv("CC_WEB_KEY", "/tmp/cc-key.pem")
    app = _make_app()
    assert app.config["SESSION_COOKIE_SECURE"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
