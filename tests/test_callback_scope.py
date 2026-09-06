"""Owned subscriptions must drain safely and leave foreign callbacks intact."""

from __future__ import annotations

import threading

import pytest

from src.core.lifecycle import CallbackScope, ScopeClosedError


def test_close_removes_only_owned_registration_and_fences_copied_callback():
    calls = []
    foreign = lambda value: calls.append(("foreign", value))
    callbacks = [foreign]
    scope = CallbackScope()
    owned = scope.register(callbacks.append, callbacks.remove, lambda value: calls.append(("owned", value)))
    owned(1)
    scope.close()
    scope.close()
    owned(2)
    assert callbacks == [foreign]
    assert calls == [("owned", 1)]
    with pytest.raises(ScopeClosedError):
        scope.register(callbacks.append, callbacks.remove, foreign)


def test_close_timeout_fences_new_calls_then_drains_and_retries_cleanup():
    started, release = threading.Event(), threading.Event()
    callbacks = []
    completed = []
    scope = CallbackScope()

    def callback():
        started.set()
        assert release.wait(5)
        completed.append(True)

    owned = scope.register(callbacks.append, callbacks.remove, callback)
    worker = threading.Thread(target=owned)
    worker.start()
    try:
        assert started.wait(5)
        with pytest.raises(TimeoutError):
            scope.close(timeout=0)
        assert scope.closed
        assert owned() is None
        assert callbacks == [owned]
    finally:
        release.set()
        worker.join(5)
    scope.close()
    assert completed == [True]
    assert callbacks == []


def test_registration_in_flight_is_removed_after_close():
    started, release = threading.Event(), threading.Event()
    callbacks = []
    scope = CallbackScope()

    def install(callback):
        callbacks.append(callback)
        started.set()
        assert release.wait(5)

    worker = threading.Thread(target=lambda: scope.register(install, callbacks.remove, lambda: None))
    worker.start()
    try:
        assert started.wait(5)
        with pytest.raises(TimeoutError):
            scope.close(timeout=0)
    finally:
        release.set()
        worker.join(5)
    scope.close()
    assert callbacks == []


def test_failing_removal_is_retryable():
    callbacks, failures = [], [True]
    scope = CallbackScope()

    def remove(callback):
        if failures.pop() if failures else False:
            raise RuntimeError("synthetic remove failure")
        callbacks.remove(callback)

    scope.register(callbacks.append, remove, lambda: None)
    with pytest.raises(RuntimeError, match="synthetic"):
        scope.close()
    scope.close()
    assert callbacks == []


def test_own_callback_cannot_deadlock_close_or_swallow_user_errors():
    scope = CallbackScope()
    with pytest.raises(RuntimeError, match="own active"):
        scope.guard(scope.close)()
    assert not scope.closed

    def fail():
        raise ScopeClosedError("application error")

    with pytest.raises(ScopeClosedError, match="application error"):
        scope.guard(fail)()


def test_failed_install_rolls_back_and_preserves_unrelated_callbacks():
    foreign = lambda: None
    callbacks = [foreign]
    scope = CallbackScope()

    def install(callback):
        callbacks.append(callback)
        raise ValueError("synthetic install failure")

    with pytest.raises(ValueError, match="synthetic"):
        scope.register(install, callbacks.remove, lambda: None)
    scope.close()
    assert callbacks == [foreign]


@pytest.mark.parametrize("failure", [ValueError, KeyboardInterrupt])
def test_failed_install_keeps_original_error_and_failed_rollback_for_retry(failure):
    scope, callbacks, seen = CallbackScope(), [], []
    original = failure("original installation failure")
    attempts = []

    def install(callback):
        callbacks.append(callback)
        raise original

    def remove(callback):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("transient rollback failure")
        callbacks.remove(callback)

    with pytest.raises(failure) as caught:
        scope.register(install, remove, lambda: seen.append(True))
    assert caught.value is original
    callbacks[0]()
    assert not seen
    scope.close()
    assert callbacks == [] and len(attempts) == 2
