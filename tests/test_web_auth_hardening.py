"""Web-remote auth hardening (deep review round 2):

- #7 a request with NO credentials must not count toward the shared lockout (else an unauthenticated
  cross-site GET, or the browser's normal pre-auth 401 handshake, can lock the owner out of the local gate).
- #5 the network surface must never trigger the physical duress wipe (allow_wipe=False).
- #6 a wildcard (0.0.0.0) bind must add the machine's real LAN origin, or LAN Socket.IO handshakes are rejected.

Isolated via CC_GATE_CONFIG (temp file).
"""

from __future__ import annotations

import base64
import socket

import pytest

pk = pytest.importorskip("src.security.physical_key")
from src.core.cross_comm import EventBus, TargetPool  # noqa: E402
from src.core.device_manager import DeviceManager  # noqa: E402
from src.core.flash_engine import FlashEngine  # noqa: E402
from src.ui.web import app as webapp  # noqa: E402


def _make_client(monkeypatch):
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")
    app, _sio = webapp.create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool(EventBus()))
    app.config.update(TESTING=True)
    return app.test_client()


def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_no_credentials_does_not_increment_lockout(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "access_gate.json"))
    client = _make_client(monkeypatch)
    for _ in range(4):
        assert client.get("/").status_code == 401  # no Authorization header
    assert pk.lockout_status()["failed_attempts"] == 0, "a no-credential request must not count"


def test_presented_wrong_credentials_still_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "access_gate.json"))
    client = _make_client(monkeypatch)
    assert client.get("/", headers=_basic("admin", "wrong")).status_code == 401
    assert pk.lockout_status()["failed_attempts"] == 1, "a presented-but-wrong credential must still count"


def test_allow_wipe_false_never_triggers_duress_wipe(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "access_gate.json"))
    calls = {"n": 0}
    monkeypatch.setattr(pk, "trigger_duress_wipe", lambda: (calls.__setitem__("n", calls["n"] + 1), True)[1])
    pk.set_wipe_on_failures(2)
    for _ in range(5):
        st = pk.record_failed_attempt(allow_wipe=False)
        assert st["wipe_triggered"] is False
    assert calls["n"] == 0, "the web path (allow_wipe=False) must never fire the duress wipe"
    # sanity: the local path (allow_wipe=True) DOES fire once past the threshold
    assert pk.record_failed_attempt(allow_wipe=True)["wipe_triggered"] is True
    assert calls["n"] == 1


def test_wildcard_bind_adds_enumerated_lan_origin(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "testhost")
    monkeypatch.setattr(socket, "gethostbyname_ex", lambda n: ("testhost", [], ["192.168.1.50"]))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])
    origins = webapp._compute_allowed_origins("0.0.0.0", 8443)
    assert "http://192.168.1.50:8443" in origins
    assert "https://192.168.1.50:8443" in origins
    assert "http://127.0.0.1:8443" in origins  # localhost still present
