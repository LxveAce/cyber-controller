"""A rate-limited serial subscribe/command must route its ``[Rate limited]`` feedback to the
request's OWN port, not a portless line both clients discard, and must reach NO subscription or
command/device call (the limiter still refuses first).

Inert: the ``@socketio.on`` closures are captured and driven directly with ``emit`` neutralized; no
socket, real port, device write or command dispatch occurs. The limiter is forced to deny.
"""
from __future__ import annotations

import pytest

pytest.importorskip("flask")

from flask import session

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.models.device import Device
from src.ui.web import app as webapp


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")


def _capture_socket_handlers(monkeypatch) -> dict:
    captured: dict = {}
    orig_on = webapp.SocketIO.on

    def patched_on(self, message, namespace=None):
        deco = orig_on(self, message, namespace=namespace)

        def capturing(handler):
            captured[message] = handler
            return deco(handler)

        return capturing

    monkeypatch.setattr(webapp.SocketIO, "on", patched_on)
    return captured


def _build(monkeypatch, *, deny):
    captured = _capture_socket_handlers(monkeypatch)
    emits: list = []
    monkeypatch.setattr(webapp, "emit", lambda *a, **k: emits.append(a))
    # Force the command limiter to deny/allow deterministically (every RateLimiter instance).
    monkeypatch.setattr(webapp.RateLimiter, "allow", lambda self, ip: not deny)
    dm = DeviceManager()
    dm.add_device(Device(port="COM4", name="Marauder", firmware="marauder", connected=True))
    calls: dict = {"get_connection": 0}
    orig_get = dm.get_connection
    monkeypatch.setattr(dm, "get_connection",
                        lambda p: (calls.__setitem__("get_connection", calls["get_connection"] + 1)
                                   or orig_get(p)))
    app, sio = webapp.create_app(dm, FlashEngine(), EventBus(), TargetPool())
    return app, captured, emits, calls


def _drive(app, handler, payload):
    with app.test_request_context(environ_base={"REMOTE_ADDR": "10.0.0.5"}):
        session["authenticated"] = True
        session["cred_gen"] = app.extensions["cc_web_credentials"].generation
        handler(payload)


def test_rate_limited_send_command_feeds_back_to_request_port_no_dispatch(monkeypatch):
    app, captured, emits, calls = _build(monkeypatch, deny=True)
    _drive(app, captured["send_command"], {"port": "COM4", "command": "scanap"})
    assert emits == [("serial_output", {"port": "COM4", "line": "[Rate limited]"})], \
        "the refusal is routed to the request's own port, not a portless line"
    assert calls["get_connection"] == 0, "a denied command reaches no device lookup/dispatch"


def test_rate_limited_subscribe_feeds_back_to_request_port_no_subscribe(monkeypatch):
    app, captured, emits, calls = _build(monkeypatch, deny=True)
    _drive(app, captured["subscribe_serial"], {"port": "COM4"})
    assert emits == [("serial_output", {"port": "COM4", "line": "[Rate limited]"})]
    assert calls["get_connection"] == 0, "a denied subscribe binds no callback"


@pytest.mark.parametrize("payload", [{}, {"port": ""}, [1, 2], "nope", None])
def test_rate_limited_absent_or_bad_port_stays_portless(monkeypatch, payload):
    # Consistent with the handler boundary: no port -> "" (never a guessed active port).
    app, captured, emits, calls = _build(monkeypatch, deny=True)
    _drive(app, captured["send_command"], payload)
    assert emits == [("serial_output", {"port": "", "line": "[Rate limited]"})]
    assert calls["get_connection"] == 0
