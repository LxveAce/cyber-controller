"""Inert tests for the async-mode-aware TLS keyword mapping in _serve_web_runtime.

No server, socket or certificate: socketio.run and make_server are doubles, and the run_simple
mismatch is established from its installed signature, not by executing it.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
import werkzeug.serving

from src.ui.web import app as webapp


def _socketio(runs, async_mode="threading"):
    return SimpleNamespace(async_mode=async_mode, run=lambda *a, **k: runs.append(k))


@pytest.fixture
def tls(monkeypatch):
    monkeypatch.delenv("CC_WEB_ALLOW_DEV_SERVER", raising=False)
    monkeypatch.delenv("CC_WEB_ALLOW_LAN", raising=False)
    monkeypatch.setenv("CC_WEB_CERT", "/tmp/cert.pem")
    monkeypatch.setenv("CC_WEB_KEY", "/tmp/key.pem")
    return monkeypatch


def _no_tls(monkeypatch):
    for n in ("CC_WEB_CERT", "CC_WEB_KEY", "CC_WEB_ALLOW_DEV_SERVER", "CC_WEB_ALLOW_LAN"):
        monkeypatch.delenv(n, raising=False)


def test_run_simple_takes_ssl_context_not_certfile_keyfile():
    # Source witness (no execution): the werkzeug entrypoint that Flask app.run -> socketio.run's
    # threading mode forwards to accepts ssl_context and has no certfile/keyfile keywords, so
    # forwarding those would raise a TypeError before serving.
    params = inspect.signature(werkzeug.serving.run_simple).parameters
    assert "ssl_context" in params
    assert "certfile" not in params and "keyfile" not in params


def test_threading_tls_without_ready_uses_ssl_context(tls):
    runs = []
    webapp._serve_web_runtime(object(), _socketio(runs), "127.0.0.1", 5000, True, ready=None)
    assert len(runs) == 1
    assert runs[0].get("ssl_context") == ("/tmp/cert.pem", "/tmp/key.pem")
    assert "certfile" not in runs[0] and "keyfile" not in runs[0]
    assert runs[0].get("allow_unsafe_werkzeug") is True


def test_threading_non_tls_passes_no_ssl(monkeypatch):
    _no_tls(monkeypatch)
    runs = []
    webapp._serve_web_runtime(object(), _socketio(runs), "127.0.0.1", 5000, True, ready=None)
    assert len(runs) == 1
    assert not any(k in runs[0] for k in ("ssl_context", "certfile", "keyfile"))


def test_non_threading_tls_preserves_the_certfile_keyfile_contract(tls):
    runs = []
    webapp._serve_web_runtime(
        object(), _socketio(runs, "eventlet"), "127.0.0.1", 5000, True, ready=None)
    assert len(runs) == 1
    assert runs[0].get("certfile") == "/tmp/cert.pem" and runs[0].get("keyfile") == "/tmp/key.pem"
    assert "ssl_context" not in runs[0]
    assert "allow_unsafe_werkzeug" not in runs[0]   # not the dev server; that flag is dev-only


def test_owned_ready_tls_uses_ssl_context_on_make_server(tls, monkeypatch):
    seen, urls = [], []

    class FakeServer:
        server_port = 8443

        def __init__(self):
            self.service_actions = lambda: None

        def serve_forever(self):
            self.service_actions()

        def server_close(self):
            pass

    monkeypatch.setattr(werkzeug.serving, "make_server",
                        lambda host, port, app, **kw: (seen.append(kw) or FakeServer()))
    ready = SimpleNamespace(serving=lambda url: urls.append(url))
    webapp._serve_web_runtime(object(), _socketio([]), "127.0.0.1", 5000, True, ready=ready)
    assert seen[0].get("ssl_context") == ("/tmp/cert.pem", "/tmp/key.pem")
    assert urls == ["https://127.0.0.1:8443"]
