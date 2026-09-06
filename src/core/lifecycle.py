"""Explicit lifetimes for callbacks installed on shared process services."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from functools import wraps
from typing import Any


class ScopeClosedError(RuntimeError):
    """New work was submitted to a scope that is closing or closed."""


def dispatch_owned(start: Callable[[Callable[[], None]], Any], work: Callable[[], None],
                   finalize: Callable[[], None]) -> Any:
    """Transfer cleanup to work atomically, even when a starter launches and then raises.

    A failed starter cancels a target that has not entered yet. Once entered, only that target
    finalizes its work. A starter retaining and later invoking a cancelled target cannot run it.
    """
    lock = threading.Lock()
    state = "pending"

    def run() -> None:
        nonlocal state
        with lock:
            if state != "pending":
                return
            state = "running"
        try:
            work()
        except BaseException as original:
            try:
                finalize()
            except BaseException as cleanup_error:
                raise original from cleanup_error
            raise
        else:
            finalize()
        finally:
            with lock:
                state = "finished"

    try:
        return start(run)
    except BaseException as original:
        with lock:
            cancelled = state == "pending"
            if cancelled:
                state = "cancelled"
        if cancelled:
            try:
                finalize()
            except BaseException as cleanup_error:
                raise original from cleanup_error
        raise


class CallbackScope:
    """Fence callbacks, drain in-flight calls, then remove exact registrations.

    Callbacks run without the condition lock held. Closing prevents new calls immediately,
    including callbacks already copied by a publisher. Existing calls finish before removal.
    A timeout leaves the fence closed and retains cleanup work for a later close attempt.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._closing = False
        self._active: dict[int, int] = {}
        self._cleanup: list[Callable[[], None]] = []
        self._close_lock = threading.Lock()

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closing

    @contextmanager
    def activity(self):
        """Reserve work that must finish before cleanup; reject admission after close."""
        self._enter()
        try:
            yield
        finally:
            self._leave()

    def _enter(self) -> None:
        ident = threading.get_ident()
        with self._condition:
            if self._closing:
                raise ScopeClosedError("callback scope is closing")
            self._active[ident] = self._active.get(ident, 0) + 1

    def _leave(self) -> None:
        ident = threading.get_ident()
        with self._condition:
            remaining = self._active[ident] - 1
            if remaining:
                self._active[ident] = remaining
            else:
                del self._active[ident]
            self._condition.notify_all()

    def guard(self, callback: Callable[..., Any], *, closed_result: Any = None) -> Callable[..., Any]:
        """Return a callback that becomes inert when this scope starts closing."""
        @wraps(callback)
        def guarded(*args, **kwargs):
            try:
                self._enter()
            except ScopeClosedError:
                return closed_result
            try:
                return callback(*args, **kwargs)
            finally:
                self._leave()

        return guarded

    def fence(self) -> None:
        """Prevent new calls now; a later close drains existing calls and removes registrations."""
        with self._condition:
            self._closing = True

    def register(
        self,
        install: Callable[[Callable[..., Any]], None],
        remove: Callable[[Callable[..., Any]], None],
        callback: Callable[..., Any],
    ) -> Callable[..., Any]:
        """Install an owned callback, retaining its exact removal operation."""
        enabled = threading.Event()
        enabled.set()

        @wraps(callback)
        def deliver(*args, **kwargs):
            if enabled.is_set():
                return callback(*args, **kwargs)
            return None

        guarded = self.guard(deliver)
        cleanup = lambda: remove(guarded)
        with self.activity():
            with self._condition:
                self._cleanup.append(cleanup)
            try:
                install(guarded)
            except BaseException as original:
                enabled.clear()
                try:
                    cleanup()
                except BaseException as rollback_error:
                    # Keep the exact cleanup token for close(), and preserve the original failure.
                    raise original from rollback_error
                with self._condition:
                    self._cleanup.remove(cleanup)
                raise
        return guarded

    def close(self, timeout: float | None = 5.0) -> None:
        """Close admission, wait for existing calls, and detach owned callbacks.

        A callback cannot close its own scope synchronously. Cleanup functions must be bounded
        and must not reenter close. A failing removal stays pending and is retried by the next call.
        """
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            if self._active.get(threading.get_ident(), 0):
                raise RuntimeError("cannot close a scope from its own active callback")
            self._closing = True
            while self._active:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("callbacks did not finish before close deadline")
                self._condition.wait(remaining)
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        acquired = self._close_lock.acquire() if remaining is None else self._close_lock.acquire(timeout=remaining)
        if not acquired:
            raise TimeoutError("another callback cleanup is still running")
        try:
            while self._cleanup:
                cleanup = self._cleanup[-1]
                cleanup()
                self._cleanup.pop()
        finally:
            self._close_lock.release()
