"""Inert wiring tests for launch_web's owned-server readiness path (src/ui/web/app.py).

No live bind, browser, device or socket: make_server is replaced with a server double, socketio.run
with a recorder, and the readiness signal + opener are doubles. Covers URL derivation, the
first-service_actions signal, the no-ready and non-threading fallbacks, bind failure, and
listener-close on normal exit / serving failure / close-also-fails.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import werkzeug.serving

from src.ui.web import app as webapp


class FakeServer:
    def __init__(self, port=54321, iterations=1, serve_error=None, close_error=None):
        self.server_port = port
        self._iterations, self._serve_error, self._close_error = iterations, serve_error, close_error
        self.serve_calls = self.closed = 0
        self.service_actions = lambda: None   # _serve_owned wraps this instance attribute

    def serve_forever(self):
        self.serve_calls += 1
        for _ in range(self._iterations):
            self.service_actions()
        if self._serve_error is not None:
            raise self._serve_error

    def server_close(self):
        self.closed += 1
        if self._close_error is not None:
            raise self._close_error


class Ready:
    def __init__(self):
        self.urls = []

    def serving(self, url):
        self.urls.append(url)


@pytest.fixture
def env(monkeypatch):
    for name in ("CC_WEB_CERT", "CC_WEB_KEY", "CC_WEB_ALLOW_DEV_SERVER", "CC_WEB_ALLOW_LAN"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _socketio(runs, async_mode="threading"):
    return SimpleNamespace(async_mode=async_mode, run=lambda *a, **k: runs.append(k))


def _make(monkeypatch, server, seen):
    def fake_make_server(host, port, app, **kw):
        seen.append((host, port, app, kw))
        if isinstance(server, BaseException):
            raise server
        return server
    monkeypatch.setattr(werkzeug.serving, "make_server", fake_make_server)


def test_ready_signals_derived_url_once_from_first_service_actions(env):
    server, seen, runs, ready = FakeServer(port=54321, iterations=3), [], [], Ready()
    _make(env, server, seen)
    rc = webapp._serve_web_runtime(object(), _socketio(runs), "127.0.0.1", 5000, True, ready=ready)
    assert rc == 0
    assert seen[0][0:2] == ("127.0.0.1", 5000) and seen[0][3]["threaded"] is True
    assert seen[0][3]["ssl_context"] is None
    assert ready.urls == ["http://127.0.0.1:54321"], "signalled once, with the real bound port"
    assert server.closed == 1 and not runs, "listener closed on normal exit; socketio.run not used"


def test_no_ready_keeps_socketio_run(env):
    seen, runs = [], []
    _make(env, FakeServer(), seen)
    rc = webapp._serve_web_runtime(object(), _socketio(runs), "127.0.0.1", 5000, True, ready=None)
    assert rc == 0 and len(runs) == 1 and not seen, "ready=None uses socketio.run, not make_server"


def test_non_threading_with_ready_falls_back_and_does_not_signal(env):
    seen, runs, ready = [], [], Ready()
    _make(env, FakeServer(), seen)
    rc = webapp._serve_web_runtime(object(), _socketio(runs, "eventlet"), "127.0.0.1", 5000, True, ready=ready)
    assert rc == 0 and len(runs) == 1 and not seen and ready.urls == [], "no owned server, no false signal"


def test_bind_failure_never_signals_and_propagates(env):
    seen, ready = [], Ready()
    _make(env, OSError("address in use"), seen)
    with pytest.raises(OSError):
        webapp._serve_web_runtime(object(), _socketio([]), "127.0.0.1", 5000, True, ready=ready)
    assert ready.urls == [], "a bind failure raises before any serving loop, so readiness never fires"


def test_serve_failure_closes_listener_and_preserves_primary(env):
    server, ready = FakeServer(serve_error=RuntimeError("serving died")), Ready()
    _make(env, server, [])
    with pytest.raises(RuntimeError, match="serving died"):
        webapp._serve_web_runtime(object(), _socketio([]), "127.0.0.1", 5000, True, ready=ready)
    assert server.closed == 1, "the listener is closed on a serving failure"
    assert ready.urls == ["http://127.0.0.1:54321"], "the first iteration still signalled before the failure"


def test_close_failure_does_not_mask_the_serving_failure(env):
    server = FakeServer(serve_error=RuntimeError("primary"), close_error=RuntimeError("close boom"))
    _make(env, server, [])
    with pytest.raises(RuntimeError, match="primary"):
        webapp._serve_web_runtime(object(), _socketio([]), "127.0.0.1", 5000, True, ready=Ready())
    assert server.closed == 1, "close was attempted; its failure did not replace the primary"


def test_tls_maps_to_ssl_context_and_https_url(env):
    env.setenv("CC_WEB_CERT", "/tmp/cert.pem")
    env.setenv("CC_WEB_KEY", "/tmp/key.pem")
    server, seen, ready = FakeServer(port=8443), [], Ready()
    _make(env, server, seen)
    webapp._serve_web_runtime(object(), _socketio([]), "127.0.0.1", 5000, True, ready=ready)
    assert seen[0][3]["ssl_context"] == ("/tmp/cert.pem", "/tmp/key.pem")
    assert ready.urls == ["https://127.0.0.1:8443"], "scheme is https and the URL uses the real port"


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", "http://127.0.0.1:7000"),
    ("0.0.0.0", "http://127.0.0.1:7000"),
    ("", "http://127.0.0.1:7000"),
    ("::", "http://[::1]:7000"),
    ("fe80::1", "http://[fe80::1]:7000"),
    ("example.local", "http://example.local:7000"),
])
def test_ready_url_display_host(host, expected):
    assert webapp._ready_url("http", host, 7000) == expected
