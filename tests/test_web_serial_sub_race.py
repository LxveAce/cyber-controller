"""Concurrency regression for the serial-subscription fan-out map (audit M-1 follow-up).

``create_app`` builds SocketIO with ``async_mode="threading"``, so two authenticated clients that
subscribe to the SAME connected port run ``on_subscribe_serial`` concurrently in separate threads.
The handler does a check-then-act on the shared ``_serial_subs`` map (read prev -> remove -> on_line
-> store). Without a lock the two threads both read ``prev`` before either stores, both register a
NEW ``on_line`` callback, and the last writer wins the map slot -> the earlier callback is orphaned
on the SerialConnection with no way to ever remove it. Every serial line then fans out to one MORE
``serial_output`` emit than there are tracked subscribers, and each racy re-subscribe leaks another
one (self-amplifying DoS).

This test forces the exact interleaving with a barrier inside the connection's ``on_line`` and proves
that after the fix exactly ONE fan-out callback survives per port. It fails (two leaked callbacks)
against the unlocked handler and passes once the critical section is made atomic per port.
"""
from __future__ import annotations

import threading

import pytest

pytest.importorskip("flask")

from flask import session

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.models.device import Device
from src.ui.web import app as webapp


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    # Same isolation the other web tests use so create_app never touches the real gate/creds.
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")


class _BarrierConn:
    """A stand-in SerialConnection that mirrors the real callback bookkeeping (append on ``on_line``,
    remove on ``remove_line_callback``) but parks inside ``on_line`` on a shared barrier.

    The barrier guarantees both subscribe threads are simultaneously inside their critical section
    (past the ``prev = _serial_subs.get(port)`` read, before the store). If the handler serializes
    the section under a lock, the second thread can never reach the barrier while the first holds the
    lock, so the first thread's ``wait`` times out and breaks the barrier — both then proceed and the
    stale callback is correctly removed, leaving exactly one.
    """

    is_connected = True

    def __init__(self, barrier: threading.Barrier) -> None:
        self._barrier = barrier
        self._lock = threading.Lock()  # guards our OWN list so the test harness is itself race-free
        self.callbacks: list = []

    def on_line(self, cb) -> None:
        try:
            self._barrier.wait()
        except threading.BrokenBarrierError:
            # Fixed handler: the lock serialized us, so the peer never arrived and the barrier broke.
            pass
        with self._lock:
            self.callbacks.append(cb)

    def remove_line_callback(self, cb) -> None:
        with self._lock:
            try:
                self.callbacks.remove(cb)
            except ValueError:
                pass


def _capture_socket_handlers(monkeypatch) -> dict:
    """Capture the raw ``@socketio.on`` closures by name so we can drive them directly from threads.

    ``SocketIO.on``'s decorator returns the original handler, so we wrap it to stash a reference.
    """
    captured: dict = {}
    orig_on = webapp.SocketIO.on

    def patched_on(self, message, namespace=None):
        deco = orig_on(self, message, namespace=namespace)

        def capturing(handler):
            captured[message] = handler
            return deco(handler)

        return capturing

    monkeypatch.setattr(webapp.SocketIO, "on", patched_on)
    return captured


def test_concurrent_subscribe_same_port_keeps_single_callback(monkeypatch):
    captured = _capture_socket_handlers(monkeypatch)
    # Neutralize the flask_socketio.emit() the handler calls for the ack line — no socket context here.
    monkeypatch.setattr(webapp, "emit", lambda *a, **k: None)

    port = "COM3"
    # timeout bounds only how long the *fixed* handler's first thread waits alone at the barrier; the
    # unfixed path meets at the barrier in microseconds so it never depends on the timeout.
    barrier = threading.Barrier(2, timeout=1.0)
    conn = _BarrierConn(barrier)

    dm = DeviceManager()
    dm.add_device(Device(port=port, name="Marauder", firmware="marauder", connected=True))
    monkeypatch.setattr(dm, "get_connection", lambda p: conn if p == port else None)

    app, sio = webapp.create_app(dm, FlashEngine(), EventBus(), TargetPool())
    subscribe = captured["subscribe_serial"]

    errors: list = []

    def worker(ip: str) -> None:
        try:
            with app.test_request_context(environ_base={"REMOTE_ADDR": ip}):
                session["authenticated"] = True
                subscribe({"port": port})
        except Exception as exc:  # pragma: no cover - reported via errors
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=("10.0.0.1",))
    t2 = threading.Thread(target=worker, args=("10.0.0.2",))
    t1.start()
    t2.start()
    t1.join(10)
    t2.join(10)

    assert not (t1.is_alive() or t2.is_alive()), "subscribe threads deadlocked"
    assert not errors, f"subscribe handler raised: {errors!r}"

    # The fix's invariant: exactly one fan-out callback registered per port. The unlocked handler
    # leaks a second, untracked callback here.
    assert len(conn.callbacks) == 1, (
        f"serial callback leak: {len(conn.callbacks)} callbacks registered for {port} (expected 1)"
    )

    # And that single callback must fan a serial line out to exactly ONE serial_output emit — proving
    # the leaked callback would otherwise double every line (the self-amplifying DoS).
    emits: list = []
    monkeypatch.setattr(sio, "emit", lambda *a, **k: emits.append(a))
    for cb in list(conn.callbacks):
        cb("chip line")
    assert len(emits) == 1, f"one serial line fanned out to {len(emits)} emits (leak amplifies output)"
