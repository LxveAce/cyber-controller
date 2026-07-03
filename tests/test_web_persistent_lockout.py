"""SEC-A1: the web login must honor the SAME persistent, restart-surviving brute-force lockout as
the console/Qt gate — not just the in-memory per-IP RateLimiter (which resets on every restart, so on
its own it lets a 'relaunch and keep guessing' attack through).

Isolated via CC_GATE_CONFIG (temp file); the opt-in duress wipe is never configured, so it can't fire.
"""

from __future__ import annotations

import base64

import pytest

pk = pytest.importorskip("src.security.physical_key")
from src.core.cross_comm import EventBus, TargetPool  # noqa: E402
from src.core.device_manager import DeviceManager  # noqa: E402
from src.core.flash_engine import FlashEngine  # noqa: E402
from src.ui.web.app import create_app  # noqa: E402


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


def _basic(user: str, pw: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_web_login_trips_persistent_lockout(tmp_path, monkeypatch):
    # fresh, isolated gate config (also carries the shared persistent failure counter)
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "access_gate.json"))
    client = _make_client(monkeypatch)

    # Burn the threshold with wrong passwords — each is a real 401, and each must increment the
    # PERSISTENT counter (not just the in-memory rate limiter).
    for _ in range(pk._LOCKOUT_AFTER):
        r = client.get("/", headers=_basic("admin", "wrong"))
        assert r.status_code == 401

    st = pk.lockout_status()
    assert st["failed_attempts"] >= pk._LOCKOUT_AFTER
    assert st["locked"] and st["remaining_secs"] > 0

    # Now even the CORRECT password is refused during the cooldown — proving the web path shares the
    # persistent lockout. (Before SEC-A1 this returned 200; the RateLimiter budget, 8/60s, is not yet
    # spent after _LOCKOUT_AFTER=5 attempts, so the 429 here is the persistent lockout, not the RL.)
    r = client.get("/", headers=_basic("admin", "test-pass-123"))
    assert r.status_code == 429
    assert b"Locked" in r.data


def test_web_login_success_resets_counter_when_not_locked(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "access_gate.json"))
    client = _make_client(monkeypatch)

    # A couple of failures (below the lockout threshold) leave a non-zero counter...
    for _ in range(2):
        assert client.get("/", headers=_basic("admin", "wrong")).status_code == 401
    assert pk.lockout_status()["failed_attempts"] == 2

    # ...and a correct login (still under the cooldown threshold) clears it, like the console path.
    r = client.get("/", headers=_basic("admin", "test-pass-123"))
    assert r.status_code == 200
    assert pk.lockout_status()["failed_attempts"] == 0
