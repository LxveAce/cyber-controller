"""Owned loopback servers and work that must finish before desktop teardown."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from src.core.lifecycle import ScopeClosedError, dispatch_owned

log = logging.getLogger(__name__)


class DesktopCleanupError(RuntimeError):
    """An old desktop runtime has not fully stopped; starting another would duplicate it."""

    def __init__(self, *args, cleanup_owner=None, secondary_errors=()):
        super().__init__(*args)
        self.cleanup_owner = cleanup_owner
        self.secondary_errors = tuple(secondary_errors[:8])

    def retry_cleanup(self):
        if self.cleanup_owner is None:
            raise RuntimeError("this cleanup error has no retry owner")
        return self.cleanup_owner.close()


def cleanup_owner_from(error):
    """Find an explicit owner through the bounded primary/control exception chain."""
    seen = set()
    for _ in range(8):
        if error is None or id(error) in seen:
            break
        seen.add(id(error))
        if isinstance(error, DesktopCleanupError) and error.cleanup_owner is not None:
            return error.cleanup_owner
        error = error.__cause__ or error.__context__
    return None


def raise_cleanup_failure(owner, errors, message):
    """Expose retained resources without formatting exceptions or masking a primary control."""
    carrier = DesktopCleanupError(message, cleanup_owner=owner, secondary_errors=errors)
    primary = errors[0]
    if not isinstance(primary, Exception):
        raise primary from carrier
    raise carrier from primary


def close_preserving_primary(owner, primary=None):
    """Use in factory rollback/finally: successful cleanup leaves the active exception untouched."""
    try:
        owner.close()
    except BaseException as cleanup_error:
        if primary is None:
            raise
        carrier = DesktopCleanupError("runtime cleanup did not complete", cleanup_owner=owner,
                                      secondary_errors=(cleanup_error,))
        if not isinstance(primary, Exception):
            raise primary from carrier
        raise carrier from primary


class WorkTracker:
    """Reserve background work before dispatch, and wait rather than interrupt its commit."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active = 0
        self._closing = False

    def reserve(self) -> Callable[[], None]:
        with self._condition:
            if self._closing:
                raise ScopeClosedError("web runtime is closing")
            self._active += 1
        released = False

        def release() -> None:
            nonlocal released
            with self._condition:
                if not released:
                    released = True
                    self._active -= 1
                    self._condition.notify_all()

        return release

    def dispatch(self, start: Callable[[Callable[[], None]], Any], work: Callable[[], None]) -> Any:
        return dispatch_owned(start, work, self.reserve())

    def close(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        with self._condition:
            self._closing = True
            if self._active:
                log.info("Waiting for %d web operation(s) to finish before closing", self._active)
            while self._active:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("web operations are still running")
                self._condition.wait(remaining)


class OwnedWebServer:
    """Bind once on loopback, retain the real port, and own the server thread and app cleanup.

    ``build(port)`` returns an app, a begin-close callback and a finish-close callback. The factory
    must clean up any partially constructed resources if it raises. No GUI or device access lives here.
    """

    def __init__(self, build: Callable[[int], tuple[Any, Callable[[], None], Callable[[], None]]]):
        from werkzeug.serving import make_server

        self._ready, self._finished = threading.Event(), threading.Event()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None
        self._closed = False
        self._launch_attempted = False
        self._thread_done = False
        self._listener_closed = False
        self._runtime_done = False
        self._nested_cleanup = None
        self._close_lock = threading.Lock()
        self._begin_close = self._finish_close = lambda: None
        self._server = make_server("127.0.0.1", 0, lambda environ, start: (), threaded=True)
        self.port = self._server.server_port
        try:
            app, self._begin_close, self._finish_close = build(self.port)
            self._server.app = app
        except BaseException as original:
            # The factory may not return, but its runtime owner must survive alongside our socket.
            self._nested_cleanup = cleanup_owner_from(original)
            close_preserving_primary(self, original)
            raise
        original_service_actions = self._server.service_actions

        def serving() -> None:
            self._ready.set()
            original_service_actions()

        self._server.service_actions = serving

    def start(self) -> None:
        if self._thread is not None or self._closed:
            raise RuntimeError("desktop server cannot be started twice")

        def run() -> None:
            try:
                if not self._stop_requested.is_set():
                    self._server.serve_forever(poll_interval=0.05)
            except BaseException as exc:
                self._failure = exc
                log.exception("Desktop web server stopped unexpectedly")
            finally:
                self._finished.set()

        try:
            self._thread = threading.Thread(target=run, name="cc-desktop-web", daemon=True)
            self._launch_attempted = True
            self._thread.start()
        except BaseException as original:
            close_preserving_primary(self, original)
            raise

    def wait_ready(self, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while not self._finished.is_set() and time.monotonic() < deadline:
            if self._ready.wait(min(0.05, max(0, deadline - time.monotonic()))):
                return not self._finished.is_set()
        return False

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def close(self) -> None:
        """Stop admission/listening, drain the app, and release only this runtime's resources."""
        with self._close_lock:
            if self._closed:
                return
            if self._thread is threading.current_thread():
                raise DesktopCleanupError("a desktop server cannot join its own serving thread",
                                          cleanup_owner=self)
            self._stop_requested.set()
            errors = []
            try:
                self._begin_close()
            except BaseException as exc:
                errors.append(exc)
            try:
                if self.alive:
                    if not self._ready.wait(5) and self.alive:
                        raise TimeoutError("desktop server did not enter its serving loop")
                    if self.alive:
                        self._server.shutdown()
            except BaseException as exc:
                errors.append(exc)
            if not self._listener_closed:
                try:
                    self._server.server_close()
                    self._listener_closed = True
                except BaseException as exc:
                    errors.append(exc)
            if not self._thread_done:
                try:
                    if self._thread is not None and self._launch_attempted:
                        # ident/is_alive false is not proof a native bootstrap never launched.
                        self._thread.join(5)
                        if self.alive:
                            raise TimeoutError("desktop server thread did not stop")
                    self._thread_done = True
                except BaseException as exc:
                    errors.append(exc)
            if not self._runtime_done:
                try:
                    if self._nested_cleanup is not None:
                        self._nested_cleanup.close()
                    else:
                        self._finish_close()
                    self._runtime_done = True
                except BaseException as exc:
                    errors.append(exc)
            if errors:
                raise_cleanup_failure(self, errors, "desktop runtime cleanup did not complete")
            self._closed = True
