"""Exact update-operation ownership using inert metadata and owned Event-controlled workers."""
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import threading
import time

import pytest

from src.core import update_checker as uc


def release(tag, **extra):
    return {'tag_name': tag, 'html_url': uc.updater.RELEASES_PAGE + '/tag/' + tag, **extra}


class Fetch:
    def __init__(self, result=None):
        self.rows = [] if result is None else result
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.error = None

    def __call__(self):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(3), 'fixture fetch was not released'
        if self.error is not None:
            raise self.error
        return self.rows


def checker(fetch, **kwargs):
    return uc.UpdateChecker('2.0.1', enabled=False, manual_cooldown_seconds=0, fetch=fetch, **kwargs)


def wait(c, view, timeout=2):
    return c.wait_operation(view.runtime_id, view.operation_id, timeout)


def finish(c, fetch):
    fetch.release.set()
    assert c.stop(3)
    assert c._thread is None or not c._thread.is_alive()


def waiter(c, view):
    entered, done = threading.Event(), threading.Event()
    values = []

    def run():
        entered.set()
        values.append(wait(c, view))
        done.set()

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(1)
    return thread, done, values


def test_queued_admission_and_pure_reads_create_no_worker():
    fetch = Fetch()
    c = checker(fetch)
    first = c.request_operation()
    assert first.outcome == uc.RequestOutcome(True, 'started')
    assert first.view.phase == 'queued' and first.view.result is None
    assert c._thread is None and fetch.calls == 0
    assert c.request_check() == uc.RequestOutcome(True, 'coalesced')
    assert c.request_operation().view == first.view
    assert wait(c, first.view, 0) == first.view
    assert c.operation_view('0' * 32, first.view.operation_id) is None
    assert c.operation_view(c.runtime_id, 'f' * 32) is None
    assert c.operation_view([], object()) is None
    assert c.stop(0)
    retired = wait(c, first.view, 0)
    assert retired.phase == 'retired' and retired.retirement_reason == 'closed'
    assert retired.result is None
    assert c.request_operation() == uc.OperationRequest(uc.RequestOutcome(False, 'closed'), None)


def test_concurrent_manual_and_legacy_callers_share_exact_worker():
    fetch = Fetch([release('v2.0.2'), release('v2.1.0'), release('v9.0.0', draft=True)])
    c = checker(fetch)
    barrier = threading.Barrier(7)
    requests = []

    def call():
        barrier.wait(2)
        requests.append(c.request_operation())

    threads = [threading.Thread(target=call) for _ in range(6)]
    try:
        c.start()
        for thread in threads:
            thread.start()
        barrier.wait(2)
        for thread in threads:
            thread.join(2)
            assert not thread.is_alive()
        assert len(requests) == 6
        assert sum(r.outcome.reason == 'started' for r in requests) == 1
        identity = requests[0].view
        assert all(r.view.operation_id == identity.operation_id for r in requests)
        assert fetch.entered.wait(1) and fetch.calls == 1
        assert c.request_check().reason == 'coalesced'
        thread, done, values = waiter(c, identity)
        assert c.operation_view(identity.runtime_id, identity.operation_id).phase == 'checking'
        fetch.release.set()
        assert done.wait(1)
        thread.join(1)
        result = values[0]
        assert result.phase == 'completed' and result.result.behind == 2
        assert result.result.latest_tag == 'v2.1.0'
        assert result.result.error_code is None and fetch.calls == 1
    finally:
        finish(c, fetch)


def test_automatic_check_has_same_operation_ownership():
    fetch = Fetch([release('v2.0.1')])
    c = uc.UpdateChecker('2.0.1', fetch=fetch)
    try:
        c.start()
        assert fetch.entered.wait(1)
        request = c.request_operation()
        assert request.outcome.reason == 'coalesced' and request.view.phase == 'checking'
        fetch.release.set()
        assert wait(c, request.view).result.behind == 0
        assert fetch.calls == 1
    finally:
        finish(c, fetch)


def test_old_terminal_timestamp_and_policy_revision_never_finish_new_queued_operation(monkeypatch):
    fetch = Fetch([release('v2.0.1')])
    fetch.release.set()
    clock = datetime(2026, 9, 6, tzinfo=timezone.utc)
    c = checker(fetch, monotonic=lambda: 0.0, wall_clock=lambda: clock)
    parked, resume = threading.Event(), threading.Event()
    original_wait = c._wake.wait

    def hold_scheduler(timeout):
        parked.set()
        assert resume.wait(3)
        return original_wait(0)

    monkeypatch.setattr(c._wake, 'wait', hold_scheduler)
    try:
        first = c.request_operation()
        c.start()
        old = wait(c, first.view)
        assert old.phase == 'completed' and parked.wait(1)
        prior_revision = c.snapshot().revision
        second = c.request_operation()
        assert second.view.operation_id != first.view.operation_id
        c.set_enabled(True)
        c.set_enabled(False)
        assert c.snapshot().revision > prior_revision
        assert c.snapshot().state == uc.UP_TO_DATE
        assert wait(c, second.view, 0).phase == 'queued'
        assert c.operation_view(c.runtime_id, first.view.operation_id) == old
        fetch.rows = [release('v2.0.2'), release('v2.0.3')]
        resume.set()
        new = wait(c, second.view)
        assert new.result.behind == 2
        assert old.result.checked_at == new.result.checked_at
        assert c.operation_view(c.runtime_id, first.view.operation_id) is None
        assert c.wait_operation(c.runtime_id, first.view.operation_id, 0) is None
    finally:
        resume.set()
        finish(c, fetch)


def test_operation_history_and_nested_results_are_bounded_and_immutable():
    fetch = Fetch([release('v2.0.2')])
    fetch.release.set()
    c = checker(fetch)
    operations = []
    try:
        c.start()
        for _ in range(5):
            request = c.request_operation()
            terminal = wait(c, request.view)
            assert terminal.phase == 'completed'
            operations.append(terminal)
        assert c._active_operation is None and c._terminal_operation == terminal
        assert all(c.operation_view(v.runtime_id, v.operation_id) is None for v in operations[:-1])
        for obj, field, value in [(terminal, 'phase', 'queued'), (terminal.result, 'behind', 900),
                                  (request.outcome, 'accepted', False)]:
            with pytest.raises(FrozenInstanceError):
                setattr(obj, field, value)
        assert c.snapshot().to_dict()['schema_version'] == 1
        assert len(uc.classify_releases('2.0.1', fetch.rows)) == 3
    finally:
        finish(c, fetch)


@pytest.mark.parametrize('failure', ['offline', 'classification', 'oversized'])
def test_unsuccessful_operation_does_not_republish_old_availability(failure):
    fetch = Fetch([release('v2.1.0')])
    fetch.release.set()
    c = checker(fetch)
    try:
        c.start()
        first = c.request_operation()
        assert wait(c, first.view).result.behind == 1
        old_tag = c.snapshot().latest_tag
        if failure == 'offline':
            fetch.error = OSError('private fixture text')
        else:
            fetch.rows = None if failure == 'classification' else [release('v2.1.0')] * 301
        second = c.request_operation()
        view = wait(c, second.view)
        assert view.result.state == (uc.OFFLINE if failure == 'offline' else uc.ERROR)
        assert view.result.behind is None and view.result.latest_tag is None
        assert view.result.latest_url is None and view.result.error_code in {uc.ERR_OFFLINE, uc.ERR_PAYLOAD, uc.ERR_TOO_LARGE}
        assert c.snapshot().latest_tag == old_tag
    finally:
        finish(c, fetch)


def test_timeout_is_bounded_with_frozen_scheduler_clock_and_does_not_cancel():
    fetch = Fetch()
    c = checker(fetch, monotonic=lambda: 0)
    request = c.request_operation()
    start = time.monotonic()
    view = wait(c, request.view, .02)
    assert .01 <= time.monotonic() - start < .5
    assert view.phase == 'queued' and c._thread is None and fetch.calls == 0
    assert c.request_operation().view.operation_id == request.view.operation_id
    c.stop()


@pytest.mark.parametrize('timeout', [-1, float('nan'), float('inf'), True, 26])
def test_wait_rejects_nonfinite_or_out_of_range_budget_without_work(timeout):
    c = checker(Fetch())
    with pytest.raises(ValueError):
        c.wait_operation(c.runtime_id, 'f' * 32, timeout)
    assert c._thread is None and c._active_operation is None


def test_stop_wakes_exact_waiter_and_retains_held_fetch_owner():
    fetch = Fetch([release('v2.1.0')])
    c = checker(fetch)
    try:
        c.start()
        request = c.request_operation()
        assert fetch.entered.wait(1)
        thread, done, values = waiter(c, request.view)
        assert c.stop(0) is False
        assert done.wait(1)
        thread.join(1)
        retired = values[0]
        assert retired.phase == 'retired' and retired.result is None
        assert c._thread.is_alive() and not c.request_operation().outcome.accepted
        fetch.release.set()
        assert c.stop(2)
        assert wait(c, request.view, 0) == retired
    finally:
        finish(c, fetch)


def test_constructor_failure_retires_prestart_admission_and_allows_explicit_retry(monkeypatch):
    fetch = Fetch()
    c = checker(fetch)
    request = c.request_operation()
    thread, done, values = waiter(c, request.view)
    original = threading.Thread
    primary = SystemExit('fixture construction control')

    def fail(*args, **kwargs):
        raise primary

    try:
        monkeypatch.setattr(threading, 'Thread', fail)
        with pytest.raises(SystemExit) as caught:
            c.start()
        assert caught.value is primary and done.wait(1)
        thread.join(1)
        assert values[0].phase == 'retired' and values[0].retirement_reason == 'start_failed'
        assert c._thread is None and not c._started
        monkeypatch.setattr(threading, 'Thread', original)
        next_request = c.request_operation()
        assert next_request.outcome.reason == 'started'
        assert next_request.view.operation_id != request.view.operation_id
        fetch.release.set()
        assert c.start() and wait(c, next_request.view).phase == 'completed'
    finally:
        monkeypatch.setattr(threading, 'Thread', original)
        finish(c, fetch)


def test_actual_native_bootstrap_error_retires_admission_without_losing_owner(monkeypatch):
    pending, allow = threading.Event(), threading.Event()
    real_thread = threading.Thread
    primary = KeyboardInterrupt('fixture interrupted native bootstrap')
    fetch = Fetch()
    c = checker(fetch)
    request = c.request_operation()

    class PendingThread(real_thread):
        def _bootstrap_inner(self):
            pending.set()
            assert allow.wait(3)
            super()._bootstrap_inner()

        def start(self):
            original = self._started.wait

            def interrupt(*args, **kwargs):
                assert pending.wait(1)
                raise primary

            self._started.wait = interrupt
            try:
                super().start()
            finally:
                self._started.wait = original

    try:
        with monkeypatch.context() as patch:
            patch.setattr(threading, 'Thread', PendingThread)
            with pytest.raises(KeyboardInterrupt) as caught:
                c.start()
        assert caught.value is primary
        owned = c._thread
        assert not owned._started.is_set() and not owned.is_alive()
        assert wait(c, request.view, 0).phase == 'retired'
        assert c.stop(0) is False and c._thread is owned
        assert c.start() is False and c.request_check().reason == 'closed'
        allow.set()
        assert owned._started.wait(1)
        assert c.stop(2) and fetch.calls == 0
    finally:
        allow.set()
        fetch.release.set()
        if c._thread is not None:
            assert c._thread._started.wait(1)
        assert c.stop(2)


@pytest.mark.parametrize('where', ['fetch', 'scheduler', 'completion_clock'])
def test_worker_control_exit_retires_exact_operation_and_preserves_primary(monkeypatch, where):
    fetch = Fetch()
    primary = SystemExit('fixture worker control')
    hook = threading.Event()
    controls = []
    c = checker(fetch)
    request = c.request_operation()

    def capture(args):
        controls.append(args.exc_value)
        hook.set()

    monkeypatch.setattr(threading, 'excepthook', capture)
    if where == 'fetch':
        fetch.error = primary
        fetch.release.set()
    elif where == 'scheduler':
        c._monotonic = lambda: (_ for _ in ()).throw(primary)
    else:
        fetch.release.set()
        c._wall = lambda: (_ for _ in ()).throw(primary)
    try:
        c.start()
        view = wait(c, request.view)
        assert view.phase == 'retired' and view.retirement_reason == 'worker_exit'
        assert hook.wait(1) and controls == [primary]
        assert c.request_operation().outcome.reason == 'closed'
    finally:
        finish(c, fetch)


def test_cooldown_outcome_has_no_old_view_or_new_identity():
    fetch = Fetch()
    fetch.release.set()
    c = uc.UpdateChecker('2.0.1', enabled=False, fetch=fetch, monotonic=lambda: 0)
    try:
        c.start()
        first = c.request_operation()
        old = wait(c, first.view)
        denied = c.request_operation()
        assert denied.outcome == uc.RequestOutcome(False, 'cooldown', 60) and denied.view is None
        assert c._active_operation is None
        assert c.operation_view(c.runtime_id, first.view.operation_id) == old
    finally:
        finish(c, fetch)


def test_start_error_after_fetch_entry_retires_exact_operation_and_retains_worker(monkeypatch):
    fetch = Fetch([release('v2.1.0')])
    c = checker(fetch)
    request = c.request_operation()
    original_start = threading.Thread.start
    primary = RuntimeError('fixture launch error after fetch entry')

    def started_then_error(thread):
        original_start(thread)
        assert fetch.entered.wait(1)
        raise primary

    try:
        with monkeypatch.context() as patch:
            patch.setattr(threading.Thread, 'start', started_then_error)
            with pytest.raises(RuntimeError) as caught:
                c.start()
        assert caught.value is primary
        owned = c._thread
        view = wait(c, request.view, 0)
        assert view.phase == 'retired' and view.retirement_reason == 'start_failed'
        assert view.result is None and c.stop(0) is False
        assert c._thread is owned and c.request_operation().outcome.reason == 'closed'
        fetch.release.set()
        assert c.stop(2) and wait(c, request.view, 0) == view
    finally:
        finish(c, fetch)
