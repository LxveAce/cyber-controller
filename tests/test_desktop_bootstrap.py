"""Desktop renderer fallback keeps an atomic, local-only, single-use bootstrap."""

from __future__ import annotations

import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import pytest

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.security import physical_key, web_auth
from src.security.desktop_bootstrap import BootstrapResult, DesktopBootstrap
from src.ui.web import app as webapp
from src.ui.web import desktop


@pytest.fixture(autouse=True)
def isolated_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "test-desktop")
    monkeypatch.setenv("CC_WEB_PASS", "synthetic-test-credential")
    monkeypatch.delenv("CC_WEB_ALLOW_LAN", raising=False)
    monkeypatch.delenv("CC_WEB_HOST_SHELL", raising=False)
    monkeypatch.delenv("CC_WEB_CERT", raising=False)
    monkeypatch.delenv("CC_WEB_KEY", raising=False)
    monkeypatch.delenv("CC_WEB_COOKIE_SECURE", raising=False)
    monkeypatch.setattr(web_auth, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(web_auth, "_WEB_AUTH_FILE", tmp_path / "web_auth.json")
    monkeypatch.setattr(web_auth, "_SECRET_KEY_FILE", tmp_path / "web_secret.key")
    monkeypatch.setattr(physical_key, "record_successful_unlock", lambda: None)


def make_app(token=None):
    app, _ = webapp.create_app(
        DeviceManager(), FlashEngine(), EventBus(), TargetPool(),
        desktop_token=token, macro_recorder=object(),
    )
    return app


def test_holder_absent_invalid_and_consumed_are_distinct():
    assert DesktopBootstrap().consume("anything") is BootstrapResult.ABSENT
    holder = DesktopBootstrap("first")
    for candidate in ("", "wrong", "é"):
        assert holder.consume(candidate) is BootstrapResult.INVALID
    assert holder.consume("first") is BootstrapResult.CONSUMED
    assert holder.consume("first") is BootstrapResult.ABSENT
    assert holder.consume("wrong") is BootstrapResult.ABSENT


def test_rotation_replaces_unused_token_and_can_follow_consumption():
    holder = DesktopBootstrap("first")
    second = holder.rotate()
    assert second != "first"
    assert holder.consume("first") is BootstrapResult.INVALID
    assert holder.consume(second) is BootstrapResult.CONSUMED
    third = holder.rotate()
    assert third != second
    assert holder.consume(second) is BootstrapResult.INVALID
    assert holder.consume(third) is BootstrapResult.CONSUMED


def test_holder_simultaneous_consumption_has_one_winner():
    holder = DesktopBootstrap("shared")
    start = threading.Barrier(12)

    def consume():
        start.wait(timeout=5)
        return holder.consume("shared")

    with ThreadPoolExecutor(max_workers=12) as workers:
        results = list(workers.map(lambda _: consume(), range(12)))
    assert results.count(BootstrapResult.CONSUMED) == 1
    assert results.count(BootstrapResult.ABSENT) == 11


@pytest.mark.parametrize("initial", ["legacy-string", DesktopBootstrap("legacy-string")])
def test_flask_preserves_string_and_holder_bootstrap_contract(initial):
    app = make_app(initial)
    client = app.test_client()
    assert client.get("/desktop-auth?token=wrong").status_code == 403
    assert client.get("/desktop-auth?token=%C3%A9").status_code == 403
    response = client.get("/desktop-auth?token=legacy-string")
    assert response.status_code == 302
    assert response.headers["Location"] == "/reform"
    assert client.get("/api/targets").status_code == 200
    with client.session_transaction() as session:
        assert session["authenticated"] is True
        assert session["csrf"]
        assert session["cred_gen"] == app.extensions["cc_web_credentials"].generation
    fresh = app.test_client()
    assert fresh.get("/desktop-auth?token=legacy-string").status_code == 404
    assert fresh.get("/api/targets").status_code == 401


def test_flask_simultaneous_consumption_has_one_authenticated_client():
    app = make_app(DesktopBootstrap("one-winner"))
    start = threading.Barrier(8)

    def request():
        client = app.test_client()
        start.wait(timeout=5)
        code = client.get("/desktop-auth?token=one-winner").status_code
        return code, client.get("/api/targets").status_code

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda _: request(), range(8)))
    assert results.count((302, 200)) == 1
    assert results.count((404, 401)) == 7


@pytest.mark.parametrize("peer", ["192.0.2.10", "2001:db8::1", "", "invalid-peer"])
def test_remote_peer_cannot_consume_bootstrap_even_with_forwarded_loopback(peer):
    app = make_app("local-secret")
    remote = app.test_client()
    response = remote.get(
        "/desktop-auth?token=local-secret",
        environ_overrides={"REMOTE_ADDR": peer},
        headers={"X-Forwarded-For": "127.0.0.1", "Forwarded": "for=127.0.0.1"},
    )
    assert response.status_code == 404
    assert "Set-Cookie" not in response.headers
    assert app.test_client().get("/desktop-auth?token=local-secret").status_code == 302


@pytest.mark.parametrize("peer", ["127.0.0.1", "127.0.0.2", "::1"])
def test_loopback_peer_can_consume_bootstrap(peer):
    client = make_app("local-secret").test_client()
    assert client.get("/desktop-auth?token=local-secret", environ_overrides={"REMOTE_ADDR": peer}).status_code == 302


def test_unconfigured_web_server_has_no_bootstrap(monkeypatch):
    monkeypatch.setenv("CC_WEB_ALLOW_LAN", "1")
    client = make_app().test_client()
    assert client.get("/desktop-auth?token=anything").status_code == 404
    assert client.get("/api/targets").status_code == 401


@pytest.mark.parametrize("unexpected", [None, "consumed", True])
def test_unexpected_holder_result_cannot_authenticate(unexpected):
    class UnexpectedHolder(DesktopBootstrap):
        def consume(self, _candidate):
            return unexpected

    client = make_app(UnexpectedHolder("local-secret")).test_client()
    assert client.get("/desktop-auth?token=local-secret").status_code == 403
    assert client.get("/api/targets").status_code == 401


@pytest.mark.parametrize("token", ["legacy-token", DesktopBootstrap("holder-token")])
@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.10", "::"])
def test_lan_launch_rejects_bootstrap_before_constructing_services(monkeypatch, token, host):
    monkeypatch.setenv("CC_WEB_ALLOW_LAN", "1")
    monkeypatch.setenv("CC_WEB_ALLOW_DEV_SERVER", "1")
    import src.core.cross_comm_hub as hub_module

    def forbidden_hub(*_a, **_kw):
        raise AssertionError("Non-loopback bootstrap must be refused before constructing services")

    monkeypatch.setattr(hub_module, "CrossCommHub", forbidden_hub)
    assert webapp.launch_web(object(), object(), object(), object(), host=host, desktop_token=token) == 2


def test_launch_web_passes_shared_holder_to_factory_unchanged(monkeypatch):
    holder = DesktopBootstrap("one")
    received = {}
    import src.core.cross_comm_hub as hub_module
    monkeypatch.setattr(hub_module, "CrossCommHub", lambda *_: types.SimpleNamespace(
        captures=object(), router=object(), sensing=object()))

    def create(*_args, **kwargs):
        received.update(kwargs)
        return types.SimpleNamespace(config={}), types.SimpleNamespace(
            async_mode="threading", run=lambda *_a, **_k: None)

    monkeypatch.setattr(webapp, "create_app", create)
    assert webapp.launch_web(object(), object(), object(), object(), desktop_token=holder) == 0
    assert received["desktop_token"] is holder
    assert received["host_shell_loopback"] is True


@pytest.mark.parametrize("native_consumes", [False, True])
def test_native_failure_rotates_token_for_fresh_browser_session(monkeypatch, native_consumes):
    """Real Flask auth routes span a simulated native failure and a different browser cookie jar."""
    state = {}

    def serve(*_a, desktop_token=None, **_kw):
        state["holder"] = desktop_token
        state["app"] = make_app(desktop_token)

    monkeypatch.setattr(webapp, "launch_web", serve)
    monkeypatch.setattr(desktop, "threading", types.SimpleNamespace(
        Thread=lambda *, target, **_kw: types.SimpleNamespace(start=target)))
    monkeypatch.setattr(desktop, "_free_loopback_port", lambda: 12345)
    monkeypatch.setattr(desktop, "_wait_until_serving", lambda *_a, **_kw: True)

    def request_path(url):
        parts = urlsplit(url)
        assert parts.hostname == "127.0.0.1" and parts.username is None and parts.password is None
        return parts.path + "?" + parts.query

    def create_window(_title, url, **_kwargs):
        state["native_url"] = url

    def native_start():
        if native_consumes:
            client = state["app"].test_client()
            assert client.get(request_path(state["native_url"])).status_code == 302
            assert client.get("/api/targets").status_code == 200
        raise RuntimeError("simulated native renderer failure")

    monkeypatch.setitem(sys.modules, "webview", types.SimpleNamespace(create_window=create_window, start=native_start))

    def browser_open(url):
        state["browser_url"] = url
        assert url != state["native_url"]
        client = state["app"].test_client()
        # Rotation rejects even an old token that never reached the server.
        assert client.get(request_path(state["native_url"])).status_code == 403
        assert client.get(request_path(url)).status_code == 302
        assert client.get("/api/targets").status_code == 200
        fresh = state["app"].test_client()
        assert fresh.get(request_path(url)).status_code == 404
        assert fresh.get(request_path(state["native_url"])).status_code == 404
        assert fresh.get("/api/targets").status_code == 401
        return True

    import webbrowser
    monkeypatch.setattr(webbrowser, "open", browser_open)

    def stop_waiting(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(desktop, "time", types.SimpleNamespace(sleep=stop_waiting))
    assert desktop.launch_desktop(object(), object(), object(), object()) == 0
    assert isinstance(state["holder"], DesktopBootstrap)
    assert state["browser_url"] != state["native_url"]
