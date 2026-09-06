"""Real loopback servers with inert devices and simulated renderers."""

from __future__ import annotations

import http.client
import json
import socket
import sys
import threading
import types
from http.cookies import SimpleCookie
from importlib.machinery import ModuleSpec
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import werkzeug.serving

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.models.device import Device
from src.security import physical_key, web_auth
from src.security.desktop_bootstrap import DesktopBootstrap
from src.ui.web import app as webapp
from src.ui.web import desktop
from src.ui.web.server_lifecycle import DesktopCleanupError, OwnedWebServer, WorkTracker


@pytest.fixture(autouse=True)
def isolated_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "lifecycle-test")
    monkeypatch.setenv("CC_WEB_PASS", "synthetic-test-credential")
    for name in ("CC_WEB_ALLOW_LAN", "CC_WEB_HOST_SHELL", "CC_WEB_CERT", "CC_WEB_KEY",
                 "CC_WEB_COOKIE_SECURE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(web_auth, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(web_auth, "_WEB_AUTH_FILE", tmp_path / "web_auth.json")
    monkeypatch.setattr(web_auth, "_SECRET_KEY_FILE", tmp_path / "web_secret.key")
    monkeypatch.setattr(physical_key, "record_successful_unlock", lambda: None)
    monkeypatch.setattr(werkzeug.serving.WSGIRequestHandler, "log_request", lambda *a, **k: None)


class InertConnection:
    port = "COM_LIFECYCLE_TEST"
    is_connected = True

    def __init__(self):
        self.lines = []

    def on_line(self, callback):
        self.lines.append(callback)

    def remove_line_callback(self, callback):
        if callback in self.lines:
            self.lines.remove(callback)

    def write(self, _command):
        raise AssertionError("No hardware command is permitted in this test")


def environment():
    dm, engine, bus = DeviceManager(), FlashEngine(), EventBus()
    pool, conn = TargetPool(bus), InertConnection()
    dm.add_device(Device(port=conn.port, firmware="marauder"))
    dm._connections[conn.port] = conn
    return dm, engine, bus, pool, conn


def request(port, path, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("GET", path, headers={"Cookie": cookie} if cookie else {})
        response = conn.getresponse()
        body = response.read()
        return response.status, response.getheader("Set-Cookie"), body
    finally:
        conn.close()


def assert_closed(server, dm, bus, conn):
    assert not server.alive
    with socket.socket() as probe:
        probe.settimeout(1)
        assert probe.connect_ex(("127.0.0.1", server.port)) != 0
    assert dm._on_conn_opened == dm._on_changed == dm._on_connected == dm._on_disconnected == []
    assert conn.lines == []
    assert conn.is_connected and dm.get_connection(conn.port) is conn
    assert all(not callbacks for topic, callbacks in bus._subscribers.items() if topic != "*")


@pytest.mark.parametrize("mode", ["normal", "native_failure", "readiness_timeout"])
def test_each_return_closes_listener_and_a_replacement_ingests_only_once(monkeypatch, mode):
    dm, engine, bus, pool, conn = environment()
    observations, servers = [], []
    bus.subscribe("*", lambda topic, data: observations.append(topic))
    create_server = webapp.create_desktop_server

    def capture_server(*args, **kwargs):
        server = create_server(*args, **kwargs)
        servers.append(server)
        return server

    monkeypatch.setattr(webapp, "create_desktop_server", capture_server)
    fake = types.ModuleType("webview")
    fake.create_window = lambda *a, **k: None

    def render():
        dm._fire_conn_opened(conn.port, conn)
        assert len(conn.lines) == 1
        observations.clear()
        for callback in tuple(conn.lines):
            callback("AP: LifecycleLab BSSID: DE:AD:BE:EF:00:11 Ch: 6 RSSI: -42")
        assert len(observations) == 1
        if mode == "native_failure":
            raise RuntimeError("synthetic renderer failure")

    fake.start = render
    monkeypatch.setitem(sys.modules, "webview", fake)
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda _url: False)
    if mode == "readiness_timeout":
        monkeypatch.setattr(OwnedWebServer, "wait_ready", lambda self: False)
    for _ in range(2):
        assert desktop.launch_desktop(dm, engine, bus, pool) == (0 if mode == "normal" else 1)
        assert_closed(servers[-1], dm, bus, conn)


def test_consumed_native_token_fallback_uses_same_server_then_closes(monkeypatch):
    dm, engine, bus, pool, conn = environment()
    servers, urls = [], []
    create_server = webapp.create_desktop_server

    def capture_server(*args, **kwargs):
        server = create_server(*args, **kwargs)
        servers.append(server)
        return server

    monkeypatch.setattr(webapp, "create_desktop_server", capture_server)

    def authenticate(url):
        urls.append(url)
        parsed = urlsplit(url)
        status, cookie, _ = request(parsed.port, parsed.path + "?" + parsed.query)
        assert status == 302
        assert request(parsed.port, "/api/targets", cookie)[0] == 200

    fake = types.ModuleType("webview")
    fake.create_window = lambda title, url, **kwargs: authenticate(url)

    def fail():
        raise RuntimeError("synthetic failure after authentication")

    fake.start = fail
    monkeypatch.setitem(sys.modules, "webview", fake)
    import webbrowser

    def open_browser(url):
        assert len(servers) == 1 and servers[0].alive
        authenticate(url)
        assert urls[0] != urls[1]
        old = urlsplit(urls[0])
        assert request(old.port, old.path + "?" + old.query)[0] == 404
        return True

    monkeypatch.setattr(webbrowser, "open", open_browser)

    def interrupt(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(desktop, "time", types.SimpleNamespace(sleep=interrupt))
    assert desktop.launch_desktop(dm, engine, bus, pool) == 0
    assert_closed(servers[0], dm, bus, conn)


@pytest.mark.parametrize("transport", ["polling", "websocket"])
def test_real_socketio_transport_disconnects_when_owned_server_closes(transport):
    import socketio
    from simple_websocket import Client, ConnectionClosed

    dm, engine, bus, pool, conn = environment()
    holder = DesktopBootstrap("synthetic-bootstrap")
    server = webapp.create_desktop_server(dm, engine, bus, pool, desktop_token=holder)
    client = None
    try:
        server.start()
        assert server.wait_ready()
        status, cookie, _ = request(server.port, "/desktop-auth?token=synthetic-bootstrap")
        assert status == 302
        app = server._server.app
        parsed = SimpleCookie(cookie)
        value = parsed[app.config["SESSION_COOKIE_NAME"]].value
        csrf = app.session_interface.get_signing_serializer(app).loads(value)["csrf"]
        if transport == "polling":
            client = socketio.Client(reconnection=False)
            disconnected = threading.Event()
            client.on("disconnect", lambda *args: disconnected.set())
            client.connect(f"http://127.0.0.1:{server.port}", headers={"Cookie": cookie},
                           auth={"csrf": csrf}, transports=["polling"], wait_timeout=5)
            assert client.connected
            server.close()
            assert disconnected.wait(3)
            client.eio.read_loop_task.join(3)
            assert not client.connected
        else:
            client = Client.connect(
                f"ws://127.0.0.1:{server.port}/socket.io/?EIO=4&transport=websocket",
                headers={"Cookie": cookie, "Origin": f"http://127.0.0.1:{server.port}"})
            assert client.receive(timeout=3).startswith("0{")
            client.send("40" + json.dumps({"csrf": csrf}))
            assert client.receive(timeout=3).startswith("40{")
            server.close()
            try:
                assert client.receive(timeout=3) == "1"  # Engine.IO close packet
            except ConnectionClosed:
                pass
        assert_closed(server, dm, bus, conn)
    finally:
        if client is not None:
            if transport == "polling":
                client.disconnect()
            elif client.connected:
                client.close()
        server.close()


def test_work_tracker_waits_for_commit_and_releases_failed_dispatch():
    tracker, begun, release = WorkTracker(), threading.Event(), threading.Event()
    finished = []

    def commit():
        begun.set()
        assert release.wait(5)
        finished.append(True)

    thread = tracker.dispatch(lambda target: _start(target), commit)
    try:
        assert begun.wait(5)
        with pytest.raises(TimeoutError):
            tracker.close(timeout=0)
        assert finished == []
    finally:
        release.set()
        thread.join(5)
    tracker.close(timeout=1)
    assert finished == [True]
    tracker = WorkTracker()

    def fail(_target):
        raise RuntimeError("synthetic thread failure")

    with pytest.raises(RuntimeError, match="synthetic"):
        tracker.dispatch(fail, lambda: None)
    tracker.close(timeout=0)


def _start(target):
    thread = threading.Thread(target=target)
    thread.start()
    return thread


def test_constructor_failure_releases_bound_socket_and_partial_callbacks(monkeypatch):
    dm, engine, bus, pool, conn = environment()
    captured = []
    make_server = werkzeug.serving.make_server

    def capture(*args, **kwargs):
        server = make_server(*args, **kwargs)
        captured.append(server)
        return server

    monkeypatch.setattr(werkzeug.serving, "make_server", capture)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic app build failure")

    monkeypatch.setattr(webapp, "create_app", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        webapp.create_desktop_server(dm, engine, bus, pool)
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", captured[0].server_port)) != 0
    assert dm._on_conn_opened == dm._on_changed == []
    assert bus.topics == []


def test_app_failure_after_event_wiring_rolls_back_owned_callbacks(monkeypatch):
    dm, engine, bus, pool, _conn = environment()

    def fail():
        raise RuntimeError("synthetic late app failure")

    monkeypatch.setattr("src.core.gps_tracker.get_tracker", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        webapp.create_desktop_server(dm, engine, bus, pool)
    assert dm._on_conn_opened == dm._on_connected == dm._on_disconnected == dm._on_changed == []
    assert bus.topics == []


def test_server_thread_start_failure_closes_listener_and_app(monkeypatch):
    from src.ui.web import server_lifecycle

    dm, engine, bus, pool, conn = environment()
    server = webapp.create_desktop_server(dm, engine, bus, pool)

    def fail():
        raise RuntimeError("synthetic thread startup failure")

    class NotStarted(threading.Thread):
        def start(self):
            fail()

    monkeypatch.setattr(server_lifecycle, "threading", types.SimpleNamespace(
        Thread=NotStarted, current_thread=threading.current_thread))
    with pytest.raises(RuntimeError, match="synthetic"):
        server.start()
    assert_closed(server, dm, bus, conn)


def test_application_close_waits_for_admitted_flash_to_finish(monkeypatch):
    dm, engine, bus, pool, conn = environment()
    started, release, committed = threading.Event(), threading.Event(), threading.Event()
    closing, closed = threading.Event(), threading.Event()
    errors = []
    monkeypatch.setattr(webapp, "_load_profiles", lambda: {"synthetic": Path("synthetic.json")})
    monkeypatch.setattr(engine, "load_profile", lambda _path: types.SimpleNamespace())
    monkeypatch.setattr(dm, "close_connection", lambda _port: None)

    def flash(*args, **kwargs):
        started.set()
        assert release.wait(5)
        committed.set()
        return True

    monkeypatch.setattr(engine, "flash", flash)
    server = webapp.create_desktop_server(dm, engine, bus, pool, desktop_token="synthetic-bootstrap")
    server.start()
    assert server.wait_ready()
    app = server._server.app
    client = app.test_client()
    assert client.get("/desktop-auth?token=synthetic-bootstrap").status_code == 302
    with client.session_transaction() as session:
        csrf = session["csrf"]
    assert client.post("/api/flash", json={"port": conn.port, "profile_id": "synthetic"},
                       headers={"X-CSRF-Token": csrf}).status_code == 200
    assert started.wait(5)

    finish_close = server._finish_close

    def observe_draining():
        closing.set()
        finish_close()

    server._finish_close = observe_draining

    def close():
        try:
            server.close()
            closed.set()
        except BaseException as exc:
            errors.append(exc)

    closer = threading.Thread(target=close)
    closer.start()
    try:
        assert closing.wait(5)
        assert not closed.is_set() and not committed.is_set()
        assert not server.alive
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", server.port)) != 0
        assert app.config["cc_hub"]._callbacks.closed
        assert app.test_client().get("/api/targets").status_code == 503
    finally:
        release.set()
        closer.join(5)
    assert not errors and committed.is_set() and closed.is_set()
    assert_closed(server, dm, bus, conn)


def test_qt_failure_before_application_creation_still_closes_server(monkeypatch):
    pytest.importorskip("PyQt5.QtWebEngineWidgets")
    from src.ui.web import desktop_qt

    dm, engine, bus, pool, conn = environment()
    servers = []
    create_server = webapp.create_desktop_server

    def capture(*args, **kwargs):
        server = create_server(*args, **kwargs)
        servers.append(server)
        return server

    def fail_before_any_window():
        raise RuntimeError("synthetic failure before QApplication or any window")

    monkeypatch.setattr(webapp, "create_desktop_server", capture)
    monkeypatch.setattr("src.ui.qt.screen.enable_high_dpi", fail_before_any_window)
    with pytest.raises(RuntimeError, match="before QApplication"):
        desktop_qt.launch_desktop_qt(dm, engine, bus, pool)
    assert len(servers) == 1
    assert_closed(servers[0], dm, bus, conn)


def test_incomplete_cleanup_does_not_start_another_backend(monkeypatch):
    from src import app as main_app

    fake = types.ModuleType("webview")
    fake.__spec__ = ModuleSpec("webview", loader=None)
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.delenv("CC_DESKTOP_SHELL", raising=False)

    def fail(*args, **kwargs):
        raise DesktopCleanupError("synthetic incomplete close")

    monkeypatch.setattr(desktop, "launch_desktop", fail)
    monkeypatch.setattr(main_app, "_launch_desktop_qt", lambda *a, **k: pytest.fail("duplicate Qt runtime"))
    monkeypatch.setattr(main_app, "_launch_web", lambda *a, **k: pytest.fail("duplicate browser runtime"))
    with pytest.raises(DesktopCleanupError):
        main_app._launch_desktop(object(), object(), object(), object())


def test_runtime_removes_serial_subscription_and_retries_failed_host_shell_close(monkeypatch):
    dm, engine, bus, pool, conn = environment()
    monkeypatch.setenv("CC_WEB_HOST_SHELL", "1")
    shells, kills = [], []
    allow_kill = threading.Event()

    class InertShell:
        def __init__(self, _output):
            shells.append(self)

        def start(self):
            pass

        def kill(self):
            if not allow_kill.is_set():
                raise RuntimeError("synthetic shell close failure")
            kills.append(self)

    monkeypatch.setattr("src.core.host_shell.HostShellSession", InertShell)
    server = webapp.create_desktop_server(dm, engine, bus, pool, desktop_token="synthetic-bootstrap")
    app = server._server.app
    socketio = app.extensions["socketio"]
    http_client = app.test_client()
    assert http_client.get("/desktop-auth?token=synthetic-bootstrap").status_code == 302
    with http_client.session_transaction() as session:
        csrf = session["csrf"]
    client = socketio.test_client(app, flask_test_client=http_client, auth={"csrf": csrf})
    try:
        server.start()
        assert server.wait_ready()
        client.emit("host_shell_open")
        client.emit("subscribe_serial", {"port": conn.port})
        assert len(shells) == len(conn.lines) == 1
        copied = conn.lines[0]
        client.get_received()
        with pytest.raises(DesktopCleanupError):
            server.close()
        assert not server.alive
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", server.port)) != 0
        allow_kill.set()
        server.close()
        assert kills == shells
        assert_closed(server, dm, bus, conn)
        copied("synthetic retired serial output")
        assert client.get_received() == []
    finally:
        allow_kill.set()
        client.disconnect()
        server.close()


def test_append_then_fail_serial_registration_is_removed_on_close():
    dm, engine, bus, pool, conn = environment()
    server = webapp.create_desktop_server(dm, engine, bus, pool, desktop_token="synthetic-bootstrap")
    app = server._server.app
    http_client = app.test_client()
    assert http_client.get("/desktop-auth?token=synthetic-bootstrap").status_code == 302
    with http_client.session_transaction() as session:
        csrf = session["csrf"]
    client = app.extensions["socketio"].test_client(
        app, flask_test_client=http_client, auth={"csrf": csrf})

    def fail_install(callback):
        conn.lines.append(callback)
        raise RuntimeError("synthetic append then fail")

    conn.on_line = fail_install
    try:
        with pytest.raises(RuntimeError, match="append then fail"):
            client.emit("subscribe_serial", {"port": conn.port})
        assert len(conn.lines) == 1
        server.close()
        assert_closed(server, dm, bus, conn)
    finally:
        client.disconnect()
        server.close()


def test_thread_stalled_before_serving_closes_listener_and_reports_incomplete_cleanup(monkeypatch):
    dm, engine, bus, pool, conn = environment()
    server = webapp.create_desktop_server(dm, engine, bus, pool)
    entered, release = threading.Event(), threading.Event()

    def stalled_loop(**kwargs):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(server._server, "serve_forever", stalled_loop)
    monkeypatch.setattr(server._ready, "wait", lambda timeout: False)
    server.start()
    try:
        assert entered.wait(5)
        with pytest.raises(DesktopCleanupError):
            server.close()
        assert server.alive  # no false success and no permission for a replacement runtime
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", server.port)) != 0
    finally:
        release.set()
        server._thread.join(5)
        server.close()
    assert_closed(server, dm, bus, conn)


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_dispatch_keeps_work_owned_if_start_raises_after_entry(failure):
    tracker = WorkTracker()
    entered, release = threading.Event(), threading.Event()
    original = failure("synthetic failure after worker entry")
    threads = []

    def work():
        entered.set()
        assert release.wait(5)

    def start(run):
        thread = _start(run)
        threads.append(thread)
        assert entered.wait(5)
        raise original

    try:
        with pytest.raises(failure) as caught:
            tracker.dispatch(start, work)
        assert caught.value is original
        with pytest.raises(TimeoutError):
            tracker.close(timeout=0)
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
        tracker.close(timeout=1)


def test_dispatch_cancels_a_late_target_after_failed_start():
    tracker, targets, executed = WorkTracker(), [], []

    def fail(run):
        targets.append(run)
        raise RuntimeError("synthetic queued but not entered")

    with pytest.raises(RuntimeError):
        tracker.dispatch(fail, lambda: executed.append(True))
    tracker.close(timeout=0)
    targets[0]()
    assert executed == []


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_server_retains_started_thread_after_start_raises(monkeypatch, failure):
    from src.ui.web import server_lifecycle

    dm, engine, bus, pool, conn = environment()
    server = webapp.create_desktop_server(dm, engine, bus, pool)
    threads = []
    original = failure("synthetic failure after server starts")

    class StartedThenRaises(threading.Thread):
        def start(self):
            threads.append(self)
            super().start()
            assert server._ready.wait(3)
            raise original

    monkeypatch.setattr(server_lifecycle, "threading", types.SimpleNamespace(
        Thread=StartedThenRaises, current_thread=threading.current_thread))
    try:
        with pytest.raises(failure) as caught:
            server.start()
        assert caught.value is original
        assert server._thread is threads[0] and not threads[0].is_alive()
        assert_closed(server, dm, bus, conn)
    finally:
        server.close()


def test_disconnect_during_shell_start_retains_cleanup_ownership(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    dm, engine, bus, pool, conn = environment()
    monkeypatch.setenv("CC_WEB_HOST_SHELL", "1")
    entered, release = threading.Event(), threading.Event()
    shells = []

    class InertShell:
        def __init__(self, _output):
            self.live = False
            shells.append(self)

        def start(self):
            entered.set()
            assert release.wait(5)
            self.live = True

        def kill(self):
            self.live = False

    monkeypatch.setattr("src.core.host_shell.HostShellSession", InertShell)
    server = webapp.create_desktop_server(dm, engine, bus, pool, desktop_token="synthetic-bootstrap")
    app = server._server.app
    http_client = app.test_client()
    assert http_client.get("/desktop-auth?token=synthetic-bootstrap").status_code == 302
    with http_client.session_transaction() as session:
        csrf = session["csrf"]
    client = app.extensions["socketio"].test_client(
        app, flask_test_client=http_client, auth={"csrf": csrf})
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            opened = executor.submit(client.emit, "host_shell_open")
            try:
                assert entered.wait(5)
                client.disconnect()
            finally:
                release.set()
            opened.result(timeout=5)
        server.close()
        assert len(shells) == 1 and not shells[0].live
        assert_closed(server, dm, bus, conn)
    finally:
        release.set()
        if client.is_connected():
            client.disconnect()
        server.close()


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit])
def test_http_macro_control_exit_releases_reserved_runtime_work(monkeypatch, tmp_path, failure):
    from src.core.macro_recorder import Macro, MacroRecorder, MacroStep
    from src.ui.web import server_lifecycle

    dm, engine, bus, pool, conn = environment()
    recorder = MacroRecorder(tmp_path / "macros")
    macro = Macro("inert", steps=[MacroStep("status")])
    monkeypatch.setattr(recorder, "list_saved_macros", lambda: [{"name": "inert", "path": "synthetic"}])
    monkeypatch.setattr(recorder, "load_macro", lambda _path: macro)
    exited, errors, trackers, finalizers = threading.Event(), [], [], []
    original = failure("synthetic HTTP macro control exit")

    def send(_command):
        raise original

    conn.write = send

    def thread_error(args):
        errors.append(args.exc_value)
        exited.set()

    monkeypatch.setattr(threading, "excepthook", thread_error)
    real_tracker, play = WorkTracker, recorder.play

    def capture_tracker():
        tracker = real_tracker()
        trackers.append(tracker)
        return tracker

    def capture_play(*args, **kwargs):
        finalizers.append(kwargs["on_exit"])
        return play(*args, **kwargs)

    monkeypatch.setattr(server_lifecycle, "WorkTracker", capture_tracker)
    monkeypatch.setattr(recorder, "play", capture_play)
    app, _socketio = webapp.create_app(dm, engine, bus, pool, macro_recorder=recorder,
                                     desktop_token="synthetic-bootstrap")
    client = app.test_client()
    assert client.get("/desktop-auth?token=synthetic-bootstrap").status_code == 302
    with client.session_transaction() as session:
        csrf = session["csrf"]
    try:
        response = client.post("/api/macros/run", json={"name": "inert", "port": conn.port},
                               headers={"X-CSRF-Token": csrf})
        assert response.status_code == 202
        assert exited.wait(5) and errors == [original]
        trackers[0].close(timeout=0)
        assert not recorder.is_playing
    finally:
        # Only a failed regression needs this fixture recovery; the worker has already exited.
        for finalize in finalizers:
            finalize()
        app.extensions["cc_begin_close"]()
        app.extensions["cc_finish_close"]()
