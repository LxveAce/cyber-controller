"""Bounded frozen-GUI packaging check using an inert Reform fixture, never operator state.

No DeviceManager, access-gate configuration, app routes, serial, flashing or acquisition services
are created. The loopback server serves only packaged assets and an empty template fixture.

Ownership of the loopback listener is decided by a lease: the serving thread claims it before
its first access and main revokes it before closing the listener itself. Exactly one side wins,
so a thread that arrives after revocation exits without touching a closed server, and main never
closes a listener that a thread is serving. A serving thread owns the close (werkzeug closes the
listener when serve_forever returns); main only asks it to stop, waits a bounded time, and reports
truthfully when the stop did not finish.
"""
from __future__ import annotations

import json
import sys
import threading

_ASSETS = ("reform.css", "reform.js", "vendor/socket.io.min.js")
_WAIT_SECONDS = 3.0


def make_fixture():
    from flask import Flask, render_template
    from flask_socketio import SocketIO

    from src.core.resources import resource_path

    static = resource_path("src", "ui", "web", "static")
    templates = resource_path("src", "ui", "web", "templates")
    for path in [templates / "reform.html", *(static / name for name in _ASSETS)]:
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError(f"Missing packaged UI resource: {path.name}")
    app = Flask("cc-packaged-smoke", static_folder=str(static), template_folder=str(templates))
    app.config["SECRET_KEY"] = "inert-packaging-fixture"
    # Exercise the real threading transport imports; no application Socket.IO handlers are bound.
    SocketIO(app, async_mode="threading", logger=False, engineio_logger=False)
    served = set()

    @app.after_request
    def observed(response):
        from flask import request
        if response.status_code == 200:
            served.add(request.path)
        return response

    @app.get("/")
    def index():
        return render_template("reform.html", csrf_token="fixture", csp_nonce="fixture",
                               devices=[], targets=[], device_count=0, target_count=0,
                               selected=None, sel_caps=[], sel_detail="", sys={},
                               flash_cats=[], flash_rows=[])

    return app, served


class _ListenerLease:
    """Who may touch the listener: ``claim`` (worker) or ``revoke`` (main), never both."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state = "open"

    def claim(self) -> bool:
        with self._lock:
            if self.state != "open":
                return False
            self.state = "claimed"
            return True

    def revoke(self) -> bool:
        with self._lock:
            if self.state != "open":
                return False
            self.state = "revoked"
            return True


class _ServeOutcome:
    def __init__(self) -> None:
        self.finished = threading.Event()
        self.error: BaseException | None = None


def _serve(server, lease: _ListenerLease, outcome: _ServeOutcome) -> None:
    try:
        if lease.claim():
            server.serve_forever()
    except BaseException as exc:  # noqa: BLE001 — kept for the report; serve_forever already closed the listener
        outcome.error = exc
    finally:
        outcome.finished.set()


def _attempt(name: str, action, errors: list[str]) -> None:
    try:
        action()
    except Exception as exc:  # noqa: BLE001 — a failing release step is recorded, never hidden
        errors.append(f"{name}: {exc!r}")


def _observe(thread, wait: float) -> str:
    """Bounded wait, then the thread's observed state. Never raises: release runs after failures."""
    try:
        thread.join(wait)
    except RuntimeError:
        return "never ran"
    except Exception as exc:  # noqa: BLE001
        return f"unobservable ({exc!r})"
    try:
        return "still alive" if thread.is_alive() else "finished"
    except Exception as exc:  # noqa: BLE001
        return f"unobservable ({exc!r})"


def _worker_note(outcome: _ServeOutcome) -> str:
    return f"; worker error: {outcome.error!r}" if outcome.error is not None else ""


def _release(server, worker, lease: _ListenerLease, outcome: _ServeOutcome, *,
             wait: float) -> tuple[str, list[str]]:
    errors: list[str] = []
    if worker is None:
        lease.revoke()
        _attempt("server_close", server.server_close, errors)
        return "listener closed by main; no worker thread", errors
    if lease.revoke():
        _attempt("server_close", server.server_close, errors)
        state = "listener closed by main; worker never claimed it"
        observed = _observe(worker, wait)
        if observed not in ("finished", "never ran"):
            state += f"; worker thread {observed}"
        return state, errors
    if outcome.finished.is_set():
        return ("complete; worker finished and closed the listener itself" + _worker_note(outcome),
                errors)
    requester = None
    launched = False
    try:
        requester = threading.Thread(target=server.shutdown, name="cc-smoke-shutdown", daemon=True)
        requester.start()
        launched = True
    except Exception as exc:  # noqa: BLE001 — best-effort helper; the worker keeps the listener
        errors.append(f"shutdown requester: {exc!r}")
    if requester is None:
        return ("unfinished; shutdown request could not be created; listener retained by worker"
                + _worker_note(outcome), errors)
    requester_state = _observe(requester, wait)
    if not launched and requester_state == "never ran":
        return ("unfinished; shutdown request never started; listener retained by worker"
                + _worker_note(outcome), errors)
    worker_state = _observe(worker, wait)
    note = _worker_note(outcome)
    if not launched:
        note += f"; shutdown requester start raised, observed afterwards: {requester_state}"
    if requester_state == "still alive":
        return (f"unfinished; shutdown request did not return within {wait:g}s; "
                f"listener retained by worker" + note, errors)
    if worker_state != "finished":
        return (f"unfinished; worker {worker_state} after the shutdown request ({wait:g}s); "
                f"listener retained by worker" + note, errors)
    return "complete; listener closed by worker" + note, errors


def run() -> int:
    if not getattr(sys, "frozen", False):
        print("Packaged startup check requires a frozen executable.", file=sys.stderr)
        return 2
    # These are the same Qt5 layer used by the Linux pywebview backend and direct fallback.
    # Import before QApplication, as required by QtWebEngine.
    from PyQt5.QtCore import QTimer, QUrl
    from PyQt5.QtWidgets import QApplication
    from qtpy.QtWebEngineWidgets import QWebEngineScript, QWebEngineView
    from werkzeug.serving import make_server

    from src.ui.launcher import LauncherDialog
    from src.ui.qt.screen import enable_high_dpi

    fixture, served = make_fixture()
    server = make_server("127.0.0.1", 0, fixture, threaded=True)
    lease = _ListenerLease()
    outcome = _ServeOutcome()
    worker = None
    view = None
    chooser = None
    result: dict[str, object] = {"status": "failed", "reason": "native renderer timed out"}
    code = 1
    failure: BaseException | None = None
    try:
        worker = threading.Thread(target=_serve, args=(server, lease, outcome),
                                  name="cc-smoke-serve", daemon=True)
        worker.start()
        enable_high_dpi()
        app = QApplication([])
        app.setQuitOnLastWindowClosed(False)
        view = QWebEngineView()
        errors = QWebEngineScript()
        errors.setInjectionPoint(QWebEngineScript.DocumentCreation)
        errors.setWorldId(QWebEngineScript.MainWorld)
        errors.setSourceCode("""
            window.__ccSmokeErrors = [];
            window.addEventListener('error', function () {
                window.__ccSmokeErrors.push('error');
            }, true);
            window.addEventListener('unhandledrejection', function () {
                window.__ccSmokeErrors.push('unhandled rejection');
            });
        """)
        view.page().scripts().insert(errors)
        chooser = LauncherDialog()
        chooser.show()
        app.processEvents()
        chooser_ok = chooser.isVisible() and not chooser.grab().isNull()
        chooser.close()
        chooser = None

        def finish(ok, reason):
            result.update(status="ok" if ok else "failed", reason=reason)
            app.exit(0 if ok else 1)

        def checked(value):
            required = {"/", *("/static/" + name for name in _ASSETS)}
            ok = value is True and chooser_ok and required <= served
            if sys.platform.startswith("linux"):
                ok = ok and app.platformName() == "xcb"
            ok = ok and view.isVisible() and not view.grab().isNull()
            finish(ok, "Reform template, styles, navigation and native rendering" if ok
                   else "Reform resource, JavaScript, chooser or native rendering check failed")

        def loaded(ok):
            if not ok:
                finish(False, "Reform document failed to load")
                return
            # This attribute and navigation handler are installed by the actual packaged reform.js.
            # A missing/broken script or stylesheet must not pass as a generic visible Qt window.
            view.page().runJavaScript("""(function () {
                var rail = document.getElementById('rail');
                var settings = document.querySelector('.navitem[data-view="settings"]');
                if (!window.__ccSmokeErrors || window.__ccSmokeErrors.length) return false;
                if (!rail || rail.getAttribute('role') !== 'tablist' || !settings) return false;
                settings.click();
                return !!document.querySelector('.view.on[data-view="settings"]') &&
                    getComputedStyle(document.documentElement)
                        .getPropertyValue('--acc').trim() !== '';
            })()""", checked)

        try:
            import pyi_splash
            pyi_splash.close()
        except ImportError:
            pass
        view.loadFinished.connect(loaded)
        view.resize(1100, 760)
        view.show()
        view.load(QUrl(f"http://127.0.0.1:{server.server_port}/"))
        QTimer.singleShot(30000, lambda: finish(False, "native renderer timed out"))
        code = app.exec_()
    except BaseException as exc:  # noqa: BLE001 — released below, then re-raised unchanged
        failure = exc

    cleanup_errors: list[str] = []
    if view is not None:
        _attempt("view.close", view.close, cleanup_errors)
    if chooser is not None:
        _attempt("chooser.close", chooser.close, cleanup_errors)
    try:
        shutdown, release_errors = _release(server, worker, lease, outcome, wait=_WAIT_SECONDS)
    except BaseException as exc:  # noqa: BLE001 — a release fault must never displace the body's failure
        shutdown = "unfinished; release failed before it could report; listener state unknown"
        release_errors = [f"release: {exc!r}"]
    cleanup_errors.extend(release_errors)

    if failure is not None:
        for note in cleanup_errors:
            failure.add_note(f"cleanup: {note}")
        failure.add_note(f"shutdown: {shutdown}")
        raise failure

    result["shutdown"] = shutdown
    if cleanup_errors:
        result["cleanup_errors"] = cleanup_errors
    clean = shutdown.startswith("complete") and outcome.error is None and not cleanup_errors
    if code == 0 and not clean:
        code = 1
        result.update(status="failed", reason="renderer check passed but release was not clean")
    print(json.dumps(result), flush=True)
    return code
