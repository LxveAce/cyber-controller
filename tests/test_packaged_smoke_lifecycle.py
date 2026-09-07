"""Ownership lifecycle of the packaged startup check, driven with in-memory doubles only.

The loopback server, the worker thread, Qt and the launcher dialog are all replaced by doubles
installed in ``sys.modules`` and on the module under test, so no socket, window, Qt object or
renderer is ever created. The server double mirrors the parts of ``socketserver`` that decide
ownership: its shutdown event starts unset, ``shutdown()`` waits on it, ``serve_forever()`` sets it
on exit, and (as werkzeug does) ``serve_forever()`` closes the listener in its own ``finally``.
The thread double returns from ``start()`` only once the worker is inside ``serve_forever``, unless
a test gates the worker to arrive late on purpose.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
import types

import pytest

from src.ui import packaged_smoke

REQUIRED = {"/", "/static/reform.css", "/static/reform.js", "/static/vendor/socket.io.min.js"}
WAIT = 0.3
VALVE = 2.0
SHUTDOWN_THREAD = "cc-smoke-shutdown"


class Recorder:
    def __init__(self):
        self.events = []
        self._lock = threading.Lock()

    def add(self, name, **info):
        with self._lock:
            self.events.append((name, info))

    def names(self):
        with self._lock:
            return [name for name, _ in self.events]


class FakeServer:
    def __init__(self, rec, script):
        self.rec = rec
        self.script = script
        self.server_port = 54321
        self.entered = threading.Event()
        self.finished = threading.Event()
        self.serve_calls = 0
        self.shutdown_calls = 0
        self.shutdown_wait_timeouts = 0
        self.close_calls_main = 0
        self.close_calls_worker = 0
        self.force = threading.Event()
        self.release_worker = threading.Event()
        self._is_shut_down = threading.Event()
        self._shutdown_request = False

    def serve_forever(self, poll_interval=0.5):
        self.serve_calls += 1
        self.rec.add("serve_forever")
        self._is_shut_down.clear()
        self.entered.set()
        try:
            if self.script.get("entry_raises"):
                raise self.script["entry_raises"]
            while not self._shutdown_request and not self.force.is_set():
                if self.script.get("loop_raises"):
                    raise self.script["loop_raises"]
                if self.script.get("loop_exits_immediately"):
                    break
                self.force.wait(0.005)
            if self._shutdown_request and self.script.get("raises_on_shutdown_request"):
                raise self.script["raises_on_shutdown_request"]
        finally:
            self._shutdown_request = False
            self._is_shut_down.set()
            self.finished.set()
            self._close(worker=True)

    def shutdown(self):
        self.shutdown_calls += 1
        self.rec.add("shutdown", entered=self.entered.is_set())
        if self.script.get("shutdown_blocks"):
            self.release_worker.wait(VALVE)
            return
        self._shutdown_request = True
        if not self._is_shut_down.wait(VALVE):
            self.shutdown_wait_timeouts += 1

    def server_close(self):
        self._close(worker=False)

    def _close(self, *, worker):
        if worker:
            self.close_calls_worker += 1
        else:
            self.close_calls_main += 1
        self.rec.add("server_close", worker=worker)
        if worker and self.script.get("worker_close_blocks"):
            self.release_worker.wait(VALVE)

    def force_stop(self):
        self.force.set()
        self._shutdown_request = True
        self.release_worker.set()


class TrackedThread(threading.Thread):
    """Real thread with a registry so every fixture thread is stopped between tests."""

    instances = []
    script = {}

    def __init__(self, *args, **kwargs):
        is_worker = kwargs.get("name") != SHUTDOWN_THREAD
        if not is_worker and self.script.get("requester_ctor_raises"):
            raise RuntimeError("requester construction failed (double)")
        super().__init__(*args, **kwargs)
        self.is_worker = is_worker
        TrackedThread.instances.append(self)

    def start(self):
        if not self.is_worker:
            mode = self.script.get("requester_start")
            if mode == "never":
                raise RuntimeError("requester never started (double; nothing launched)")
            super().start()
            if mode == "raise-after-launch":
                raise RuntimeError("requester start raised after launching (double)")
            return
        mode = self.script.get("worker_start")
        if mode == "never":
            raise RuntimeError("can't start new thread (double; nothing launched)")
        super().start()
        server = self.script.get("server")
        if server is not None and self.script.get("worker_gate") is None:
            assert server.entered.wait(VALVE), "worker did not enter serve_forever within the valve"
        if mode == "raise-after-launch":
            raise RuntimeError("start raised after launching the worker (double)")
        if mode == "interrupt-after-launch":
            raise KeyboardInterrupt("interrupted while waiting for the worker to start (double)")

    def run(self):
        gate = self.script.get("worker_gate") if self.is_worker else None
        if gate is not None:
            gate.wait()
        super().run()


class BrokenThread:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("thread construction failed (double)")


class _Pixmap:
    def isNull(self):
        return False


class _Signal:
    def __init__(self, store):
        self._store = store

    def connect(self, callback):
        self._store.append(callback)


def build_doubles(rec, script):
    def mod(name, **attrs):
        m = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(m, key, value)
        return m

    timers = []
    servers = []
    views = []
    apps = []
    real_qtwidgets = sys.modules.get("PyQt5.QtWidgets")

    class QTimer:
        @staticmethod
        def singleShot(ms, callback):  # noqa: N802 (Qt name)
            timers.append((ms, callback))

    class QUrl:
        def __init__(self, url):
            self.url = url

    class QWebEngineScript:
        DocumentCreation = 0
        MainWorld = 0

        def setInjectionPoint(self, value):  # noqa: N802
            pass

        def setWorldId(self, value):  # noqa: N802
            pass

        def setSourceCode(self, source):  # noqa: N802
            self.source = source

    class _Scripts:
        def insert(self, script_obj):
            if script.get("scripts_insert_raises"):
                raise script["scripts_insert_raises"]
            rec.add("scripts.insert")

    class _Page:
        def __init__(self):
            self._scripts = _Scripts()

        def scripts(self):
            return self._scripts

        def runJavaScript(self, js, callback):  # noqa: N802
            callback(script.get("js_result", True))

    class QWebEngineView:
        def __init__(self):
            if script.get("view_ctor_raises"):
                raise script["view_ctor_raises"]
            self._page = _Page()
            self.load_callbacks = []
            self.loadFinished = _Signal(self.load_callbacks)
            self.visible = False
            views.append(self)
            rec.add("QWebEngineView")

        def page(self):
            return self._page

        def resize(self, w, h):
            pass

        def show(self):
            self.visible = True

        def isVisible(self):  # noqa: N802
            return self.visible

        def grab(self):
            return _Pixmap()

        def load(self, url):
            rec.add("view.load")
            if script.get("view_load_raises"):
                raise script["view_load_raises"]

        def close(self):
            rec.add("view.close")
            if script.get("view_close_raises"):
                raise script["view_close_raises"]

    class QApplication:
        def __init__(self, argv):
            if script.get("app_ctor_raises"):
                raise script["app_ctor_raises"]
            self.exit_codes = []
            apps.append(self)
            rec.add("QApplication")

        @classmethod
        def instance(cls):
            if real_qtwidgets is None:
                return None
            return real_qtwidgets.QApplication.instance()

        def setQuitOnLastWindowClosed(self, value):  # noqa: N802
            pass

        def processEvents(self):  # noqa: N802
            pass

        def platformName(self):  # noqa: N802
            return "double"

        def exit(self, code=0):
            self.exit_codes.append(code)

        def exec_(self):
            rec.add("app.exec_")
            if script.get("exec_raises"):
                raise script["exec_raises"]
            if script.get("exec_waits_for_worker_finish"):
                for thread in TrackedThread.instances:
                    if thread.is_worker and thread.ident is not None:
                        thread.join(VALVE)
            for view in views:
                for callback in view.load_callbacks:
                    callback(script.get("load_ok", True))
            return self.exit_codes[-1] if self.exit_codes else 0

    class LauncherDialog:
        def __init__(self):
            if script.get("launcher_ctor_raises"):
                raise script["launcher_ctor_raises"]
            self.visible = False
            rec.add("LauncherDialog")

        def show(self):
            self.visible = True

        def isVisible(self):  # noqa: N802
            return self.visible

        def grab(self):
            return _Pixmap()

        def close(self):
            self.visible = False

    def enable_high_dpi():
        if script.get("high_dpi_raises"):
            raise script["high_dpi_raises"]
        return True

    def make_server(host, port, app, threaded=False, **kw):
        if script.get("make_server_raises"):
            raise script["make_server_raises"]
        server = FakeServer(rec, script)
        servers.append(server)
        script["server"] = server
        rec.add("make_server")
        return server

    modules = {
        "PyQt5": mod("PyQt5"),
        "PyQt5.QtCore": mod("PyQt5.QtCore", QTimer=QTimer, QUrl=QUrl),
        "PyQt5.QtWidgets": mod("PyQt5.QtWidgets", QApplication=QApplication),
        "qtpy": mod("qtpy"),
        "qtpy.QtWebEngineWidgets": mod("qtpy.QtWebEngineWidgets", QWebEngineScript=QWebEngineScript,
                                       QWebEngineView=QWebEngineView),
        "werkzeug": mod("werkzeug"),
        "werkzeug.serving": mod("werkzeug.serving", make_server=make_server),
        "src.ui.launcher": mod("src.ui.launcher", LauncherDialog=LauncherDialog),
        "src.ui.qt": mod("src.ui.qt"),
        "src.ui.qt.screen": mod("src.ui.qt.screen", enable_high_dpi=enable_high_dpi),
        "pyi_splash": mod("pyi_splash", close=lambda: rec.add("pyi_splash.close")),
    }
    modules["PyQt5"].QtCore = modules["PyQt5.QtCore"]
    modules["PyQt5"].QtWidgets = modules["PyQt5.QtWidgets"]
    modules["qtpy"].QtWebEngineWidgets = modules["qtpy.QtWebEngineWidgets"]
    modules["src.ui.qt"].screen = modules["src.ui.qt.screen"]
    handles = types.SimpleNamespace(servers=servers, views=views, apps=apps, timers=timers)
    return modules, handles


@pytest.fixture
def smoke(monkeypatch):
    rec = Recorder()
    script = {}
    modules, handles = build_doubles(rec, script)
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(packaged_smoke, "make_fixture", lambda: (object(), set(REQUIRED)))
    monkeypatch.setattr(packaged_smoke, "_WAIT_SECONDS", WAIT, raising=False)
    TrackedThread.instances.clear()
    TrackedThread.script = script
    thread_ns = types.SimpleNamespace(Thread=TrackedThread, Event=threading.Event,
                                      Lock=threading.Lock)
    monkeypatch.setattr(packaged_smoke, "threading", thread_ns)
    out = io.StringIO()

    def run():
        with contextlib.redirect_stdout(out):
            return packaged_smoke.run()

    def run_expecting(exc_type):
        with contextlib.redirect_stdout(out):
            with pytest.raises(exc_type) as info:
                packaged_smoke.run()
        return info.value

    def result():
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        assert lines, "run() prints one JSON result line"
        return json.loads(lines[-1])

    def server():
        assert len(handles.servers) == 1
        return handles.servers[0]

    def worker_stopped():
        for thread in TrackedThread.instances:
            if thread.is_worker and thread.ident is not None:
                thread.join(VALVE)
                if thread.is_alive():
                    return False
        return True

    api = types.SimpleNamespace(rec=rec, script=script, handles=handles, run=run,
                                run_expecting=run_expecting, result=result, server=server,
                                worker_stopped=worker_stopped, thread_ns=thread_ns)
    yield api
    gate = script.get("worker_gate")
    if gate is not None:
        gate.set()
    for srv in handles.servers:
        srv.force_stop()
    leftovers = []
    for thread in TrackedThread.instances:
        if thread.ident is not None:
            thread.join(VALVE)
            if thread.is_alive():
                leftovers.append(thread.name)
    TrackedThread.instances.clear()
    assert leftovers == [], f"fixture threads still alive after the test: {leftovers}"


def assert_released_by_worker(smoke, srv):
    assert srv.shutdown_calls == 1, "a serving worker is asked to shut down exactly once"
    assert srv.shutdown_wait_timeouts == 0, "shutdown never waits past the double's valve"
    assert smoke.worker_stopped(), "the worker thread has stopped"
    assert srv.close_calls_worker == 1, "the worker closed the listener in serve_forever"
    assert srv.close_calls_main == 0, "main never closes a listener the worker owns"


def assert_released_by_main(smoke, srv):
    assert srv.serve_calls == 0, "the server was never served"
    assert srv.shutdown_calls == 0, "no shutdown request without a worker to answer it"
    assert srv.close_calls_main == 1, "main closes the listener exactly once"
    assert srv.close_calls_worker == 0


# Controls: current semantics that must not change.

def test_success_path_reports_ok_and_releases_in_order(smoke):
    assert smoke.run() == 0
    result = smoke.result()
    assert result["status"] == "ok"
    assert result["shutdown"].startswith("complete")
    names = smoke.rec.names()
    assert names.index("serve_forever") < names.index("app.exec_")
    assert names.index("view.close") < names.index("shutdown")
    assert_released_by_worker(smoke, smoke.server())
    assert smoke.handles.timers[0][0] == 30000


def test_not_frozen_returns_2_without_allocating(smoke, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    with contextlib.redirect_stderr(io.StringIO()):
        assert smoke.run() == 2
    assert smoke.rec.names() == []


def test_document_load_failure_reports_failed_and_releases(smoke):
    smoke.script["load_ok"] = False
    assert smoke.run() == 1
    result = smoke.result()
    assert result["status"] == "failed"
    assert "failed to load" in result["reason"]
    assert_released_by_worker(smoke, smoke.server())


def test_failure_inside_event_loop_propagates_after_release(smoke):
    smoke.script["exec_raises"] = RuntimeError("event loop aborted (double)")
    smoke.run_expecting(RuntimeError)
    assert_released_by_worker(smoke, smoke.server())


def test_make_server_failure_allocates_nothing_else(smoke):
    smoke.script["make_server_raises"] = OSError("bind refused (double)")
    smoke.run_expecting(OSError)
    assert smoke.handles.servers == []
    assert smoke.handles.apps == []
    assert smoke.handles.views == []


# Failures after the worker started and before the event loop.

@pytest.mark.parametrize("key", ["high_dpi_raises", "app_ctor_raises", "view_ctor_raises",
                                 "scripts_insert_raises", "launcher_ctor_raises"])
def test_failure_after_worker_start_releases_server_and_propagates(smoke, key):
    smoke.script[key] = RuntimeError(f"{key} (double)")
    exc = smoke.run_expecting(RuntimeError)
    assert key in str(exc)
    assert_released_by_worker(smoke, smoke.server())
    assert any(note.startswith("shutdown: complete") for note in exc.__notes__)


# Cleanup failures never replace the original error or stop the release.

def test_original_error_survives_view_close_failure(smoke):
    smoke.script["view_load_raises"] = ValueError("navigation refused (double)")
    smoke.script["view_close_raises"] = RuntimeError("view close failed (double)")
    exc = smoke.run_expecting(ValueError)
    assert "navigation refused" in str(exc)
    assert any("view.close" in note for note in exc.__notes__)
    assert_released_by_worker(smoke, smoke.server())


def test_cleanup_failure_on_success_path_is_reported_and_nonzero(smoke):
    smoke.script["view_close_raises"] = RuntimeError("view close failed (double)")
    assert smoke.run() == 1
    result = smoke.result()
    assert result["status"] == "failed"
    assert any("view.close" in item for item in result["cleanup_errors"])
    assert_released_by_worker(smoke, smoke.server())


# Worker start uncertainty: the lease decides, never elapsed time.

def test_thread_construction_failure_main_closes_listener(smoke):
    smoke.thread_ns.Thread = BrokenThread
    exc = smoke.run_expecting(RuntimeError)
    assert "thread construction failed" in str(exc)
    assert_released_by_main(smoke, smoke.server())
    assert smoke.handles.apps == []


def test_never_launched_worker_main_closes_listener(smoke):
    smoke.script["worker_start"] = "never"
    exc = smoke.run_expecting(RuntimeError)
    assert "nothing launched" in str(exc)
    assert_released_by_main(smoke, smoke.server())
    assert smoke.handles.apps == []


def test_start_raised_after_launch_worker_keeps_listener(smoke):
    smoke.script["worker_start"] = "raise-after-launch"
    exc = smoke.run_expecting(RuntimeError)
    assert "after launching" in str(exc)
    assert_released_by_worker(smoke, smoke.server())


def test_interrupted_start_worker_keeps_listener(smoke):
    smoke.script["worker_start"] = "interrupt-after-launch"
    smoke.run_expecting(KeyboardInterrupt)
    assert_released_by_worker(smoke, smoke.server())


def test_worker_arriving_after_revocation_never_touches_the_server(smoke):
    gate = threading.Event()
    smoke.script["worker_gate"] = gate
    smoke.script["launcher_ctor_raises"] = RuntimeError("chooser construction failed (double)")
    smoke.run_expecting(RuntimeError)
    srv = smoke.server()
    assert srv.close_calls_main == 1, "main revoked and closed before the late worker ran"
    gate.set()
    assert smoke.worker_stopped()
    assert srv.serve_calls == 0, "the late worker observed the revocation and never served"
    assert srv.shutdown_calls == 0


# Workers that finish on their own, with or without errors.

def test_finished_worker_gets_no_shutdown_request(smoke):
    smoke.script["loop_exits_immediately"] = True
    smoke.script["exec_waits_for_worker_finish"] = True
    assert smoke.run() == 0
    srv = smoke.server()
    assert srv.shutdown_calls == 0, "no shutdown request for a worker that already finished"
    assert srv.close_calls_main == 0
    assert srv.close_calls_worker == 1
    assert smoke.result()["shutdown"].startswith("complete")


@pytest.mark.parametrize("key,text", [("entry_raises", "selector registration failed"),
                                      ("loop_raises", "accept failed")])
def test_worker_error_is_preserved_and_fails_the_check(smoke, key, text):
    smoke.script[key] = OSError(f"{text} (double)")
    smoke.script["exec_waits_for_worker_finish"] = True
    with contextlib.redirect_stderr(io.StringIO()):
        assert smoke.run() == 1
    result = smoke.result()
    assert result["status"] == "failed"
    assert text in result["shutdown"]
    srv = smoke.server()
    assert srv.shutdown_calls == 0
    assert srv.close_calls_main == 0


# Unfinished shutdown is reported, never claimed complete.

def test_blocked_worker_close_is_reported_unfinished(smoke):
    smoke.script["worker_close_blocks"] = True
    assert smoke.run() == 1
    result = smoke.result()
    assert result["status"] == "failed"
    assert result["shutdown"].startswith("unfinished")
    assert smoke.server().close_calls_main == 0, "main never forces a close the worker still owns"


def test_shutdown_request_that_never_returns_is_reported_unfinished(smoke):
    smoke.script["shutdown_blocks"] = True
    assert smoke.run() == 1
    result = smoke.result()
    assert result["status"] == "failed"
    assert "shutdown request did not return" in result["shutdown"]
    assert smoke.server().close_calls_main == 0


# The shutdown requester is best effort: its failures never escape, never displace the body's
# error, never close a worker-owned listener, and are reported from what was observed afterwards.

def assert_worker_still_serving(srv):
    assert srv.entered.is_set() and not srv.finished.is_set()
    assert srv.close_calls_main == 0, "main never closes a listener the worker owns"


def test_requester_construction_failure_keeps_body_error(smoke):
    smoke.script["requester_ctor_raises"] = True
    smoke.script["launcher_ctor_raises"] = RuntimeError("chooser construction failed (double)")
    exc = smoke.run_expecting(RuntimeError)
    assert "chooser construction failed" in str(exc)
    assert any("shutdown requester" in note for note in exc.__notes__)
    assert any(note.startswith("shutdown: unfinished") for note in exc.__notes__)
    assert_worker_still_serving(smoke.server())


def test_requester_construction_failure_is_reported_unfinished(smoke):
    smoke.script["requester_ctor_raises"] = True
    assert smoke.run() == 1
    result = smoke.result()
    assert result["status"] == "failed"
    assert "could not be created" in result["shutdown"]
    assert any("shutdown requester" in item for item in result["cleanup_errors"])
    assert_worker_still_serving(smoke.server())


def test_requester_never_started_keeps_body_error(smoke):
    smoke.script["requester_start"] = "never"
    smoke.script["view_ctor_raises"] = RuntimeError("renderer init failed (double)")
    exc = smoke.run_expecting(RuntimeError)
    assert "renderer init failed" in str(exc)
    assert any("never started" in note for note in exc.__notes__)
    assert_worker_still_serving(smoke.server())


def test_requester_never_started_is_reported_unfinished(smoke):
    smoke.script["requester_start"] = "never"
    assert smoke.run() == 1
    result = smoke.result()
    assert "never started" in result["shutdown"]
    assert_worker_still_serving(smoke.server())


def test_requester_start_raised_after_launch_reports_observed_outcome(smoke):
    smoke.script["requester_start"] = "raise-after-launch"
    assert smoke.run() == 1
    result = smoke.result()
    srv = smoke.server()
    assert srv.shutdown_calls == 1, "the launched requester did deliver the shutdown request"
    assert smoke.worker_stopped()
    assert result["shutdown"].startswith("complete")
    assert "observed afterwards: finished" in result["shutdown"]
    assert any("after launching" in item for item in result["cleanup_errors"])
    assert srv.close_calls_main == 0


def test_requester_start_raised_after_launch_keeps_body_error(smoke):
    smoke.script["requester_start"] = "raise-after-launch"
    smoke.script["high_dpi_raises"] = RuntimeError("high-dpi attribute failed (double)")
    exc = smoke.run_expecting(RuntimeError)
    assert "high-dpi attribute failed" in str(exc)
    assert any(note.startswith("shutdown: complete") for note in exc.__notes__)
    assert any("after launching" in note for note in exc.__notes__)
    assert smoke.worker_stopped()


def test_worker_error_raised_during_shutdown_is_reported_after_the_join(smoke):
    smoke.script["raises_on_shutdown_request"] = OSError("listener vanished on shutdown (double)")
    with contextlib.redirect_stderr(io.StringIO()):
        assert smoke.run() == 1
    result = smoke.result()
    assert result["status"] == "failed"
    assert "listener vanished on shutdown" in result["shutdown"]
    assert smoke.server().close_calls_main == 0


def test_release_fault_never_displaces_body_error(smoke, monkeypatch):
    def broken_release(*args, **kwargs):
        raise RuntimeError("release exploded (double)")

    monkeypatch.setattr(packaged_smoke, "_release", broken_release)
    smoke.script["launcher_ctor_raises"] = RuntimeError("chooser construction failed (double)")
    exc = smoke.run_expecting(RuntimeError)
    assert "chooser construction failed" in str(exc)
    assert any("release exploded" in note for note in exc.__notes__)


def test_release_fault_on_success_path_is_reported(smoke, monkeypatch):
    def broken_release(*args, **kwargs):
        raise RuntimeError("release exploded (double)")

    monkeypatch.setattr(packaged_smoke, "_release", broken_release)
    assert smoke.run() == 1
    result = smoke.result()
    assert "release failed" in result["shutdown"]
    assert any("release exploded" in item for item in result["cleanup_errors"])


@pytest.mark.parametrize("stage", ["constructor", "start"])
def test_release_with_a_failing_requester_never_raises(monkeypatch, stage):
    lease = packaged_smoke._ListenerLease()
    assert lease.claim()
    outcome = packaged_smoke._ServeOutcome()
    closes = []
    server = types.SimpleNamespace(shutdown=lambda: None, server_close=lambda: closes.append(1))

    class Requester:
        def __init__(self, **kwargs):
            if stage == "constructor":
                raise RuntimeError("inert requester construction failed")

        def start(self):
            raise RuntimeError("inert requester start failed")

    monkeypatch.setattr(packaged_smoke, "threading", types.SimpleNamespace(Thread=Requester))
    state, errors = packaged_smoke._release(server, object(), lease, outcome, wait=0.01)
    assert state.startswith("unfinished")
    assert errors and "inert requester" in errors[0]
    assert closes == []


# The lease itself.

def test_lease_is_exclusive():
    lease = packaged_smoke._ListenerLease()
    assert lease.claim() is True
    assert lease.revoke() is False
    assert lease.claim() is False
    other = packaged_smoke._ListenerLease()
    assert other.revoke() is True
    assert other.claim() is False
