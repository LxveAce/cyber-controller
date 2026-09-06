"""Real manual routes and cleanup ownership with inert fetches/listeners/renderers."""
from __future__ import annotations

from datetime import datetime, timezone
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest

from src.config import settings
from src.core import update_checker as uc
from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.core.lifecycle import CallbackScope
from src.security import web_auth
from src.ui.web import app as webapp
from src.ui.web.server_lifecycle import (
    DesktopCleanupError, OwnedWebServer, cleanup_owner_from, close_preserving_primary,
)
from src.ui.web.update_runtime import UpdateAvailability, WebRuntimeCleanup


STAMP = datetime(2026, 9, 6, 19, tzinfo=timezone.utc)


def release(tag):
    return {"tag_name": tag, "html_url": uc.updater.RELEASES_PAGE + "/tag/" + tag}


class Fetch:
    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.rows = [release("v2.0.1")]
        self.error = None
        self.calls = 0

    def __call__(self):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(15), "inert fetch was not released"
        if self.error is not None:
            raise self.error
        return self.rows


class Hub:
    captures = router = sensing = None

    def __init__(self, *args):
        self.fences = self.closes = 0

    def fence(self):
        self.fences += 1

    def close(self):
        self.closes += 1


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(web_auth, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(web_auth, "_WEB_AUTH_FILE", tmp_path / "web_auth.json")
    monkeypatch.setattr(web_auth, "_SECRET_KEY_FILE", tmp_path / "web_secret.key")
    monkeypatch.setenv("CC_WEB_USER", "fixture")
    monkeypatch.setenv("CC_WEB_PASS", "inert-availability-password")
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    return tmp_path


def authenticated(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(authenticated=True, cred_gen=app.extensions["cc_web_credentials"].generation,
                       csrf="fixture")
    return client


def post(app, body, path="/api/updates/check"):
    return authenticated(app).post(path, json=body, headers={"X-CSRF-Token": "fixture"})


def status(app, view):
    return authenticated(app).get("/api/updates/status", query_string={
        "runtime_id": view["runtime_id"], "operation_id": view["operation_id"]})


def invoke(call):
    result = {}

    def run():
        try:
            result["value"] = call()
        except BaseException as error:
            result["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def settle(thread, result):
    thread.join(3)
    assert not thread.is_alive()
    if "error" in result:
        raise result["error"]
    return result["value"]


@pytest.fixture
def environment(isolated):
    fetch = Fetch()
    checker = uc.UpdateChecker("2.0.1", enabled=False, manual_cooldown_seconds=0,
                               fetch=fetch, wall_clock=lambda: STAMP)
    scope, hub = CallbackScope(), Hub()
    owner = WebRuntimeCleanup(scope, hub)
    owner.checker = checker
    adapter = UpdateAvailability(checker)
    app, _ = webapp.create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool(),
                              lifecycle=scope, availability=adapter)
    owner.attach_app(app)
    yield SimpleNamespace(app=app, fetch=fetch, checker=checker, adapter=adapter,
                          owner=owner, hub=hub, scope=scope)
    fetch.release.set()
    owner.close()
    assert checker._thread is None or not checker._thread.is_alive()


def complete(env, view):
    env.fetch.release.set()
    result = env.checker.wait_operation(view["runtime_id"], view["operation_id"], 2)
    assert result.phase == "completed"
    return status(env.app, view).get_json()


def test_absent_owner_pure_reads_and_rejected_admission(isolated):
    app, _ = webapp.create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool())
    try:
        assert authenticated(app).get("/api/version").status_code == 200
        for body in ({}, {"schema_version": 2}):
            response = post(app, body)
            assert response.status_code == 503
            assert response.get_json()["error"] == "availability_unavailable"
            assert response.headers["Cache-Control"] == "no-store"
        response = status(app, {"runtime_id": "a" * 32, "operation_id": "b" * 32})
        assert response.status_code == 503
    finally:
        app.extensions["cc_begin_close"]()
        app.extensions["cc_finish_close"]()


def test_auth_csrf_and_protocol_rejections_create_no_work(environment):
    env = environment
    assert env.app.test_client().post("/api/updates/check", json={"schema_version": 2}).status_code == 401
    assert authenticated(env.app).post("/api/updates/check", json={"schema_version": 2}).status_code == 403
    for body in ([], {"schema_version": True}, {"schema_version": 1}, {"url": "https://invalid.invalid"}):
        assert post(env.app, body).status_code == 400
    bad = authenticated(env.app).get("/api/updates/status?runtime_id=x&operation_id=x")
    assert bad.status_code == 400 and bad.headers["Cache-Control"] == "no-store"
    assert authenticated(env.app).get("/api/updates/status?runtime_id=" + "a" * 32 +
                                     "&operation_id=" + "b" * 32 + "&operation_id=" + "c" * 32).status_code == 400
    assert env.checker._thread is None and env.checker._active_operation is None and env.fetch.calls == 0


def test_prompt_admission_coalesces_clients_and_get_is_pure(environment, monkeypatch):
    env = environment
    monkeypatch.setattr(settings, "patch_settings", lambda *a, **k: pytest.fail("schema2 wrote settings"))
    env.checker.start()
    assert env.fetch.calls == 0
    began = time.monotonic()
    first = post(env.app, {"schema_version": 2})
    assert first.status_code == 202 and time.monotonic() - began < 1
    view = first.get_json()
    assert view["accepted"] is True and view["reason"] == "started"
    assert view["phase"] == "queued" and view["result"] is None
    assert env.fetch.entered.wait(1)
    assert env.scope._active == {}
    calls = [invoke(lambda: post(env.app, {"schema_version": 2})) for _ in range(4)]
    for thread, result in calls:
        other = settle(thread, result).get_json()
        assert other["operation_id"] == view["operation_id"] and other["reason"] == "coalesced"
    for _ in range(3):
        read = status(env.app, view)
        assert read.status_code == 200 and read.get_json()["phase"] == "checking"
        assert read.get_json()["result"] is None and "accepted" not in read.get_json()
    assert env.fetch.calls == 1
    env.fetch.rows = [release("v2.0.2"), release("v2.0.3")]
    terminal = complete(env, view)
    assert terminal["result"]["behind"] == 2 and terminal["result"]["status"] == "NEWER"
    assert terminal["retirement_reason"] is None


def test_old_terminal_same_timestamp_and_policy_revision_do_not_complete_queued_request(environment, monkeypatch):
    env = environment
    parked, resume = threading.Event(), threading.Event()

    def hold(_timeout):
        parked.set()
        assert resume.wait(3)
        return False

    monkeypatch.setattr(env.checker._wake, "wait", hold)
    first = post(env.app, {"schema_version": 2}).get_json()
    env.fetch.release.set()
    env.checker.start()
    try:
        old = complete(env, first)
        assert parked.wait(1)
        next_view = post(env.app, {"schema_version": 2}).get_json()
        env.checker.set_enabled(True)
        env.checker.set_enabled(False)
        assert status(env.app, next_view).get_json()["phase"] == "queued"
        assert status(env.app, next_view).get_json()["result"] is None
        assert status(env.app, first).get_json() == old
        env.fetch.rows = [release("v2.0.2"), release("v2.0.3")]
        resume.set()
        assert complete(env, next_view)["result"]["behind"] == 2
        assert status(env.app, first).status_code == 410
        wrong = dict(next_view, runtime_id="0" * 32)
        assert status(env.app, wrong).status_code == 409
    finally:
        resume.set()


@pytest.mark.parametrize("failure", ["offline", "classification"])
def test_failed_new_operation_never_carries_previous_release(environment, failure):
    env = environment
    env.fetch.rows = [release("v2.0.2")]
    first = post(env.app, {"schema_version": 2}).get_json()
    env.checker.start()
    assert complete(env, first)["result"]["status"] == "NEWER"
    if failure == "offline":
        env.fetch.error = OSError("inert offline")
    else:
        env.fetch.rows = None
    next_view = post(env.app, {"schema_version": 2}).get_json()
    answer = complete(env, next_view)
    assert env.checker.snapshot().latest_tag == "v2.0.2"
    if failure == "offline":
        assert answer["result"] == {"ok": False, "status": "OFFLINE", "current": "2.0.1"}
    else:
        assert answer["result"] is None and answer["error"] == "classification_error"


def test_legacy_timeout_does_not_cancel_owner_or_release_other_clients(environment):
    env = environment
    env.adapter.legacy_wait_seconds = 0.02
    env.checker.start()
    response = post(env.app, {})
    assert response.status_code == 503 and response.get_json()["error"] == "check_timeout"
    assert env.fetch.calls == 1 and not env.checker._closed
    joined = post(env.app, {"schema_version": 2}).get_json()
    assert joined["reason"] == "coalesced"
    assert complete(env, joined)["result"]["behind"] == 0


def test_close_wakes_legacy_lease_before_app_drain_and_rejects_new_work(environment):
    env = environment
    env.checker.start()
    thread, result = invoke(lambda: post(env.app, {}))
    try:
        assert env.fetch.entered.wait(1) and env.scope._active
        env.owner.begin_close()
        response = settle(thread, result)
        assert response.status_code == 503 and response.get_json()["error"] == "retired"
        assert env.scope._active == {} and env.checker._thread.is_alive()
        assert post(env.app, {"schema_version": 2}).status_code == 503
        assert env.fetch.calls == 1
    finally:
        env.fetch.release.set()
        settle(thread, result)


def test_cooldown_uses_admission_retry_without_an_old_result(environment):
    env = environment
    env.checker._cooldown = 60
    env.checker.start()
    first = post(env.app, {"schema_version": 2}).get_json()
    complete(env, first)
    response = post(env.app, {"schema_version": 2})
    assert response.status_code == 429 and response.get_json()["error"] == "cooldown"
    assert int(response.headers["Retry-After"]) == response.get_json()["retry_after_seconds"]
    assert "operation_id" not in response.get_json() and env.fetch.calls == 1


@pytest.mark.parametrize("reset", [False, True])
def test_legacy_bookkeeping_preserves_acknowledged_settings(environment, reset):
    env = environment
    settings.save_settings({"serial": {"default_baud": 230400}, "uploads": {"wigle_token": "old"}})
    env.checker.start()
    thread, result = invoke(lambda: post(env.app, {}))
    try:
        assert env.fetch.entered.wait(1)
        body = {"reset": True} if reset else {"serial": {"default_baud": 9600},
            "interface": {"touch_mode": "on"}, "updates": {"enabled": False},
            "uploads": {"wigle_token": "new"}}
        assert post(env.app, body, "/api/settings").status_code == 200
        acknowledged = settings.load_settings()
    finally:
        env.fetch.release.set()
        response = settle(thread, result)
    assert response.status_code == 200 and response.get_json()["status"] == "UP_TO_DATE"
    after = settings.load_settings()
    for section in ("serial", "interface", "uploads", "safety", "vault"):
        assert after[section] == acknowledged[section]
    assert after["updates"]["enabled"] == acknowledged["updates"]["enabled"]
    assert after["updates"]["last_check_iso"] == "2026-09-06T19:00:00+00:00"
    assert after["updates"]["last_seen_latest"] == "v2.0.1"
    assert env.checker.snapshot().enabled is False


def test_legacy_offline_preserves_previous_tag_and_best_effort_save_failure(environment, monkeypatch):
    env = environment
    settings.save_settings({"updates": {"last_seen_latest": "v2.0.1"}})
    env.fetch.error = OSError("inert offline")
    env.fetch.release.set()
    env.checker.start()
    response = post(env.app, {})
    assert response.status_code == 200 and response.get_json() == {
        "ok": False, "status": "OFFLINE", "current": "2.0.1"}
    assert settings.load_settings()["updates"]["last_seen_latest"] == "v2.0.1"
    assert settings.load_settings()["updates"]["last_check_iso"]
    monkeypatch.setattr(settings, "patch_settings", lambda *a, **k: (_ for _ in ()).throw(OSError("inert")))
    assert post(env.app, {}).status_code == 200


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), True, 26])
def test_compatibility_wait_budget_is_finite_and_bounded(bad):
    with pytest.raises(ValueError):
        UpdateAvailability(object(), legacy_wait_seconds=bad)


def test_checker_cleanup_precedes_unbounded_app_stage_and_hub_waits_for_dependency():
    scope, hub = CallbackScope(), Hub()
    owner = WebRuntimeCleanup(scope, hub)
    order = []
    owner.checker = SimpleNamespace(stop=lambda timeout: order.append(("checker", timeout)) or True)
    failure = RuntimeError("app drain incomplete")

    def finish():
        order.append(("app", None))
        raise failure

    owner.app_finish = finish
    with pytest.raises(DesktopCleanupError) as caught:
        owner.close()
    assert caught.value.cleanup_owner is owner
    assert order[0][0] == "checker" and hub.closes == 0 and not owner.closed
    owner.app_finish = lambda: order.append(("app_retry", None))
    caught.value.retry_cleanup()
    assert owner.closed and hub.closes == 1
    before = list(order)
    owner.close()
    assert order == before and hub.closes == 1


def test_failed_fence_does_not_skip_checker_or_independent_cleanup():
    scope, hub = CallbackScope(), Hub()
    owner = WebRuntimeCleanup(scope, hub)
    controls = []
    primary = KeyboardInterrupt("fence control")
    owner.app_begin = lambda: (_ for _ in ()).throw(primary)
    owner.app_finish = lambda: controls.append("app_done")
    owner.checker = SimpleNamespace(stop=lambda timeout: controls.append(timeout) or True)
    with pytest.raises(KeyboardInterrupt) as caught:
        owner.close()
    assert caught.value is primary and cleanup_owner_from(primary) is owner
    assert 0 in controls and "app_done" in controls and hub.closes == 1
    owner.app_begin = lambda: None
    cleanup_owner_from(primary).close()
    assert owner.closed and hub.closes == 1


@pytest.mark.parametrize("control", [False, True])
def test_primary_is_preserved_while_cleanup_owner_remains_retriable(control):
    primary = KeyboardInterrupt("primary") if control else RuntimeError("primary")
    cleanup = RuntimeError("cleanup")
    owner = SimpleNamespace(close=lambda: (_ for _ in ()).throw(cleanup))
    with pytest.raises(KeyboardInterrupt if control else DesktopCleanupError) as caught:
        close_preserving_primary(owner, primary)
    error = caught.value
    assert (error is primary) if control else (error.__cause__ is primary)
    assert cleanup_owner_from(error) is owner
    owner.close = lambda: None
    cleanup_owner_from(error).close()


class InertListener:
    server_port = 12345

    def __init__(self):
        self.closes = 0
        self.fail_close = False
        self.service_actions = lambda: None

    def server_close(self):
        self.closes += 1
        if self.fail_close:
            raise RuntimeError("inert listener close failure")


@pytest.mark.parametrize("control", [False, True])
def test_factory_failure_retains_outer_listener_and_inner_runtime(isolated, monkeypatch, control):
    import werkzeug.serving
    listener = InertListener()
    listener.fail_close = True
    monkeypatch.setattr(werkzeug.serving, "make_server", lambda *a, **k: listener)
    inner = SimpleNamespace(close=lambda: (_ for _ in ()).throw(RuntimeError("inner pending")))
    carrier = DesktopCleanupError("factory incomplete", cleanup_owner=inner)
    primary = KeyboardInterrupt("factory control") if control else carrier
    if control:
        primary.__cause__ = carrier

    def build(_port):
        raise primary

    with pytest.raises(KeyboardInterrupt if control else DesktopCleanupError) as caught:
        OwnedWebServer(build)
    if control:
        assert caught.value is primary
    outer = cleanup_owner_from(caught.value)
    assert outer is not inner and outer._nested_cleanup is inner and outer._server is listener
    assert not outer._closed and listener.closes == 1
    inner.close = lambda: None
    listener.fail_close = False
    outer.close()
    assert outer._closed and outer._listener_closed and outer._runtime_done


def test_actual_native_server_bootstrap_retains_owner_until_join(monkeypatch):
    import werkzeug.serving
    from src.ui.web import server_lifecycle
    listener = InertListener()
    monkeypatch.setattr(werkzeug.serving, "make_server", lambda *a, **k: listener)
    pending, release_bootstrap = threading.Event(), threading.Event()
    primary = KeyboardInterrupt("start wait control")
    real_thread = threading.Thread
    owned = []

    class PendingBootstrap(real_thread):
        def _bootstrap_inner(self):
            pending.set()
            assert release_bootstrap.wait(5)
            super()._bootstrap_inner()

        def start(self):
            owned.append(self)
            original_wait = self._started.wait

            def interrupted(_timeout=None):
                assert pending.wait(2)
                raise primary

            self._started.wait = interrupted
            try:
                super().start()
            finally:
                self._started.wait = original_wait

    server = OwnedWebServer(lambda _port: (object(), lambda: None, lambda: None))
    try:
        with monkeypatch.context() as context:
            context.setattr(server_lifecycle.threading, "Thread", PendingBootstrap)
            with pytest.raises(KeyboardInterrupt) as caught:
                server.start()
        assert caught.value is primary and cleanup_owner_from(primary) is server
        assert server._thread is owned[0] and not owned[0]._started.is_set()
        assert not server.alive and not server._closed and server._listener_closed
        with pytest.raises(RuntimeError):
            server.start()
    finally:
        release_bootstrap.set()
        for thread in owned:
            assert thread._started.wait(2)
            thread.join(2)
            assert not thread.is_alive()
        server.close()
    assert server._closed


def test_real_factory_manual_start_and_close_create_one_checker_no_fetch(isolated, monkeypatch):
    from src.core import cross_comm_hub
    created = []
    fetch = Fetch()
    real_checker = uc.UpdateChecker

    def construct(*args, **kwargs):
        assert kwargs == {"enabled": False}
        checker = real_checker(*args, **kwargs, fetch=fetch)
        created.append(checker)
        return checker

    monkeypatch.setattr(cross_comm_hub, "CrossCommHub", Hub)
    monkeypatch.setattr(uc, "UpdateChecker", construct)
    app, _socketio, begin, finish = webapp._build_web_runtime(
        DeviceManager(), FlashEngine(), EventBus(), TargetPool(), host="127.0.0.1", port=12345,
        audit=None, desktop_token=None)
    owner = app.config["cc_runtime_cleanup"]
    try:
        assert len(created) == 1 and created[0]._thread is not None and fetch.calls == 0
        assert owner.checker is created[0]
        begin()
        assert post(app, {"schema_version": 2}).status_code == 503
    finally:
        fetch.release.set()
        finish()
    assert owner.closed and not created[0]._thread.is_alive()


@pytest.mark.parametrize("point", ["construct", "app", "start"])
@pytest.mark.parametrize("control", [False, True])
def test_factory_rollback_preserves_primary_when_cleanup_succeeds(isolated, monkeypatch, point, control):
    from src.core import cross_comm_hub
    primary = SystemExit("factory primary") if control else RuntimeError("factory primary")
    hubs = []

    def hub(*args):
        instance = Hub()
        hubs.append(instance)
        return instance

    def fail(*args, **kwargs):
        raise primary

    monkeypatch.setattr(cross_comm_hub, "CrossCommHub", hub)
    if point == "construct":
        monkeypatch.setattr(uc, "UpdateChecker", fail)
    elif point == "app":
        monkeypatch.setattr(webapp, "create_app", fail)
    else:
        monkeypatch.setattr(uc.UpdateChecker, "start", fail)
    with pytest.raises(type(primary)) as caught:
        webapp._build_web_runtime(DeviceManager(), FlashEngine(), EventBus(), TargetPool(),
            host="127.0.0.1", port=12345, audit=None, desktop_token=None)
    assert caught.value is primary and hubs[0].closes == 1


def test_web_finalizer_preserves_body_control_and_cleanup_owner(isolated, monkeypatch):
    primary = KeyboardInterrupt("serve control")
    owner = SimpleNamespace(close=lambda: (_ for _ in ()).throw(RuntimeError("cleanup pending")))
    app = SimpleNamespace(config={"cc_runtime_cleanup": owner})
    monkeypatch.setattr(webapp, "_build_web_runtime", lambda *a, **k: (app, object(), lambda: None, owner.close))
    monkeypatch.setattr(webapp, "_serve_web_runtime", lambda *a: (_ for _ in ()).throw(primary))
    with pytest.raises(KeyboardInterrupt) as caught:
        webapp.launch_web(object(), object(), object(), object())
    assert caught.value is primary and cleanup_owner_from(primary) is owner


def test_desktop_finalizer_preserves_body_control_and_cleanup_owner(isolated, monkeypatch):
    from src.ui.web import desktop
    primary = KeyboardInterrupt("renderer control")
    server = SimpleNamespace(start=lambda: None, wait_ready=lambda: True, port=12345,
                             close=lambda: (_ for _ in ()).throw(RuntimeError("cleanup pending")))
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace())
    monkeypatch.setattr(webapp, "create_desktop_server", lambda *a, **k: server)
    monkeypatch.setattr(desktop, "_run_desktop_window", lambda *a: (_ for _ in ()).throw(primary))
    with pytest.raises(KeyboardInterrupt) as caught:
        desktop.launch_desktop(object(), object(), object(), object())
    assert caught.value is primary and cleanup_owner_from(primary) is server


def test_qt_finalizer_preserves_body_control_without_creating_a_window(isolated, monkeypatch):
    pytest.importorskip("PyQt5.QtWebEngineWidgets")
    from src.ui.web import desktop_qt
    primary = SystemExit("Qt startup control")
    server = SimpleNamespace(start=lambda: (_ for _ in ()).throw(primary),
                             close=lambda: (_ for _ in ()).throw(RuntimeError("cleanup pending")))
    monkeypatch.setattr(webapp, "create_desktop_server", lambda *a, **k: server)
    with pytest.raises(SystemExit) as caught:
        desktop_qt.launch_desktop_qt(object(), object(), object(), object())
    assert caught.value is primary and cleanup_owner_from(primary) is server


def test_qt_finalizer_with_inert_import_surface_preserves_actual_start_control(isolated, monkeypatch):
    from src.ui.web import desktop_qt
    # These names are never instantiated: the exact launcher fails at server.start before Qt setup.
    names = {
        "QtCore": "QFile QIODevice QObject QSettings Qt QUrl pyqtSlot",
        "QtGui": "QDesktopServices QKeySequence", "QtWebChannel": "QWebChannel",
        "QtWebEngineWidgets": "QWebEnginePage QWebEngineScript QWebEngineSettings QWebEngineView",
        "QtWidgets": "QAction QApplication QFileDialog QMainWindow QMenu QSystemTrayIcon",
    }
    primary = SystemExit("inert Qt startup control")
    server = SimpleNamespace(start=lambda: (_ for _ in ()).throw(primary),
                             close=lambda: (_ for _ in ()).throw(RuntimeError("cleanup pending")))
    # Restore imports before the repository's autouse Qt reaper runs during fixture teardown.
    with monkeypatch.context() as imports:
        parent = ModuleType("PyQt5")
        parent.__path__ = []
        imports.setitem(sys.modules, "PyQt5", parent)
        for suffix, members in names.items():
            module = ModuleType("PyQt5." + suffix)
            for name in members.split():
                setattr(module, name, object())
            imports.setitem(sys.modules, "PyQt5." + suffix, module)
        imports.setattr(webapp, "create_desktop_server", lambda *a, **k: server)
        with pytest.raises(SystemExit) as caught:
            desktop_qt.launch_desktop_qt(object(), object(), object(), object())
    assert caught.value is primary and cleanup_owner_from(primary) is server


@pytest.mark.parametrize("control", [False, True])
def test_factory_started_worker_error_retains_cleanup_until_released(isolated, monkeypatch, control):
    from src.core import cross_comm_hub
    primary = KeyboardInterrupt("reported start failure") if control else RuntimeError("reported start failure")
    fetch, checkers = Fetch(), []
    real_checker, real_thread = uc.UpdateChecker, threading.Thread

    class EnteredThenError(real_thread):
        def start(self):
            super().start()
            assert fetch.entered.wait(2)
            raise primary

    class Checker(real_checker):
        def start(self):
            self.request_operation()  # Inert pre-start admission makes the real worker enter our held fetch.
            with monkeypatch.context() as context:
                context.setattr(uc.threading, "Thread", EnteredThenError)
                return super().start()

    def construct(*args, **kwargs):
        instance = Checker(*args, **kwargs, fetch=fetch)
        checkers.append(instance)
        return instance

    monkeypatch.setattr(cross_comm_hub, "CrossCommHub", Hub)
    monkeypatch.setattr(uc, "UpdateChecker", construct)
    try:
        with pytest.raises(KeyboardInterrupt if control else DesktopCleanupError) as caught:
            webapp._build_web_runtime(DeviceManager(), FlashEngine(), EventBus(), TargetPool(),
                host="127.0.0.1", port=12345, audit=None, desktop_token=None)
        if control:
            assert caught.value is primary
        else:
            assert caught.value.__cause__ is primary
        owner = cleanup_owner_from(caught.value)
        assert owner.checker is checkers[0] and checkers[0]._thread.is_alive()
        assert not owner.closed and owner._app_done and owner._hub_done
        assert checkers[0].request_operation().outcome.reason == "closed"
        assert checkers[0]._terminal_operation.phase == "retired"
        fetch.release.set()
        owner.close()
        assert owner.closed and not checkers[0]._thread.is_alive()
    finally:
        fetch.release.set()
        for checker in checkers:
            assert checker.stop(3)


def test_worker_control_retirement_has_no_result_and_cannot_restart(environment, monkeypatch):
    env = environment
    primary = SystemExit("inert worker control")
    seen, stopped = [], threading.Event()

    def hook(args):
        seen.append(args.exc_value)
        stopped.set()

    monkeypatch.setattr(threading, "excepthook", hook)
    env.fetch.error = primary
    view = post(env.app, {"schema_version": 2}).get_json()
    env.checker.start()
    env.fetch.release.set()
    assert stopped.wait(2) and seen == [primary]
    result = status(env.app, view).get_json()
    assert result["phase"] == "retired" and result["error"] == "retired"
    assert result["retirement_reason"] == "worker_exit" and result["result"] is None
    assert post(env.app, {"schema_version": 2}).status_code == 503
    assert not env.checker.start() and env.fetch.calls == 1
