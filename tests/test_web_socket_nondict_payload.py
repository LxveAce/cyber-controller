"""A non-dict SocketIO payload must be ignored cleanly, not crash the handler.

The HTTP routes coerce any non-object body to {} via _json_body() so `.get(...)` can't AttributeError,
but the socket handlers used the `(data or {}).get(...)` idiom, which only rescues FALSY payloads — a
truthy non-dict (e.g. [1,2] or a bare string) reaches .get() on a list and raises AttributeError,
logging a server-side traceback and dropping the event. This drives the raw handlers with non-dict
payloads and asserts they return without raising (same capture pattern as test_web_serial_sub_race)."""
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


@pytest.mark.parametrize("payload", [[1, 2], "hello", 5, True])
@pytest.mark.parametrize("event", ["send_command", "subscribe_serial"])
def test_socket_handlers_ignore_non_dict_payload(monkeypatch, event, payload):
    captured = _capture_socket_handlers(monkeypatch)
    monkeypatch.setattr(webapp, "emit", lambda *a, **k: None)

    dm = DeviceManager()
    dm.add_device(Device(port="COM3", name="Marauder", firmware="marauder", connected=True))
    app, _sio = webapp.create_app(dm, FlashEngine(), EventBus(), TargetPool())
    handler = captured[event]

    with app.test_request_context(environ_base={"REMOTE_ADDR": "10.0.0.9"}):
        session["authenticated"] = True
        # Was: AttributeError: 'list' object has no attribute 'get' (etc.) out of the handler.
        handler(payload)  # must not raise; the malformed payload is treated as an empty {} and ignored
