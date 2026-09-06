"""Cleanup ownership across worker interruption; all process and device effects are inert."""

from __future__ import annotations

import threading
import types

import pytest

from src.core.host_shell import HostShellSession
from src.core.lifecycle import dispatch_owned
from src.core.macro_recorder import Macro, MacroRecorder, MacroStep


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit])
def test_dispatch_preserves_control_exception_when_finalize_fails(failure):
    original, cleanup_error = failure("control interruption"), RuntimeError("cleanup failure")

    def work():
        raise original

    def finalize():
        raise cleanup_error

    with pytest.raises(failure) as caught:
        dispatch_owned(lambda run: run(), work, finalize)
    assert caught.value is original and caught.value.__cause__ is cleanup_error


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("async_", [False, True])
def test_macro_finalizes_control_exit_independently_of_outcome(monkeypatch, tmp_path, failure, async_):
    recorder = MacroRecorder(tmp_path)
    macro = Macro("inert", steps=[MacroStep("status")])
    original = failure("synthetic playback interruption")
    completed, finalized, errors = [], [], []
    exited = threading.Event()

    def send(_command):
        raise original

    def on_exit():
        finalized.append(True)

    def thread_error(args):
        errors.append(args.exc_value)
        exited.set()

    monkeypatch.setattr(threading, "excepthook", thread_error)
    kwargs = dict(send_command=send, complete_callback=lambda *a: completed.append(a),
                  on_exit=on_exit, async_=async_)
    if async_:
        recorder.play(macro, **kwargs)
        assert exited.wait(5)
        assert errors == [original]
    else:
        with pytest.raises(failure) as caught:
            recorder.play(macro, **kwargs)
        assert caught.value is original
    assert not recorder.is_playing
    assert completed == [] and finalized == [True]


def test_declined_macro_finalizes_without_clearing_another_playback(tmp_path):
    recorder = MacroRecorder(tmp_path)
    recorder._playing = True
    finalized = []
    recorder.play(Macro("inert"), lambda command: pytest.fail("no commands expected"),
                  on_exit=lambda: finalized.append(True))
    assert recorder.is_playing and finalized == [True]


@pytest.mark.parametrize("after_entry", [False, True])
def test_macro_start_failure_finalizes_only_when_work_stops(monkeypatch, tmp_path, after_entry):
    from src.core import macro_recorder

    recorder = MacroRecorder(tmp_path)
    entered, release = threading.Event(), threading.Event()
    finalized, threads = [], []
    original = RuntimeError("synthetic macro thread startup failure")

    def send(_command):
        entered.set()
        assert release.wait(5)

    class FailingStart(threading.Thread):
        def start(self):
            threads.append(self)
            if after_entry:
                super().start()
                assert entered.wait(5)
            raise original

    monkeypatch.setattr(macro_recorder, "threading", types.SimpleNamespace(Thread=FailingStart))
    try:
        with pytest.raises(RuntimeError) as caught:
            recorder.play(Macro("inert", steps=[MacroStep("status")]), send,
                          on_exit=lambda: finalized.append(True))
        assert caught.value is original
        assert recorder.is_playing is after_entry
        assert finalized == ([] if after_entry else [True])
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(5)
    assert not recorder.is_playing and finalized == [True]


def test_real_shell_kill_failure_retains_process_for_retry():
    session = HostShellSession(lambda _text: None)

    class InertProcess:
        code = None
        refuse = True

        def poll(self):
            return self.code

        def terminate(self):
            if self.refuse:
                raise PermissionError("synthetic termination failure")
            self.code = 0

        def wait(self, **kwargs):
            return self.code

    process = InertProcess()
    session._proc = process
    with pytest.raises(PermissionError):
        session.kill()
    assert session._proc is process and session.is_alive
    process.refuse = False
    session.kill()
    assert session._proc is None and not session.is_alive
    session.kill()
