"""Captured Mesh read ownership through delivery/retirement; inert real-reader controls."""
import threading

import pytest
from test_meshtastic_serial_provider import eventually, node
from test_meshtastic_serial_provider import prepared as prepared


def ready(prepared):
    conn, session, fake = prepared()
    conn.connect()
    eventually(lambda: session.snapshot()['config_complete'] and session.backend._pump_owner is None)
    return conn, session, fake


def close_while_held(conn, session, release):
    errors = []
    def close():
        try:
            conn.disconnect()
        except BaseException as exc:
            errors.append(exc)
    closer = threading.Thread(target=close)
    closer.start()
    try:
        eventually(lambda: session.snapshot()['retired'])
        with pytest.raises(RuntimeError):
            session.export_state()
    finally:
        release.set()
        closer.join(3)
    assert not closer.is_alive() and not errors
    stream = conn._managed_stream
    stream.reader_thread.join(3)
    assert not stream.reader_thread.is_alive() and stream.rx_batch is None
    state = session.export_state()
    assert state.wire.issued == 2 and state.wire.outstanding_id is None
    return state


def test_retirement_before_delivery_claim_retains_captured_loss(prepared, monkeypatch):
    conn, session, fake = ready(prepared)
    entered, release = threading.Event(), threading.Event()
    original = conn._deliver_managed_batch
    def gated(*args):
        assert conn._managed_stream.rx_batch is None
        entered.set()
        assert release.wait(3)
        return original(*args)
    monkeypatch.setattr(conn, '_deliver_managed_batch', gated)
    fake.enqueue(node(777))
    assert entered.wait(3)
    state = close_while_held(conn, session, release)
    assert state.wire.resync_required and conn._managed_stream.lost
    assert 777 not in session.backend.nodes and len(fake.writes) == 3


def test_retirement_inside_owner_feed_gate_retains_loss(prepared, monkeypatch):
    conn, session, fake = ready(prepared)
    entered, release = threading.Event(), threading.Event()
    original = session._owner.feed_bytes
    def gated(*args):
        assert conn._managed_stream.rx_batch is not None
        entered.set()
        assert release.wait(3)
        return original(*args)
    monkeypatch.setattr(session._owner, 'feed_bytes', gated)
    fake.enqueue(node(777))
    assert entered.wait(3)
    state = close_while_held(conn, session, release)
    assert state.wire.resync_required and conn._managed_stream.lost
    assert 777 not in session.backend.nodes and len(fake.writes) == 3


def test_retirement_after_consumption_before_delivery_release_is_conservative_loss(prepared, monkeypatch):
    conn, session, fake = ready(prepared)
    entered, release = threading.Event(), threading.Event()
    original = session._receive
    def gated(*args):
        result = original(*args)
        if 777 in session.backend.nodes:
            assert session.backend._pump_owner is None
            assert not session.backend._framer.buffered
            assert conn._managed_stream.rx_batch is not None
            entered.set()
            assert release.wait(3)
        return result
    monkeypatch.setattr(session, '_receive', gated)
    fake.enqueue(node(777))
    assert entered.wait(3)
    state = close_while_held(conn, session, release)
    assert 777 in session.backend.nodes
    assert state.wire.resync_required and conn._managed_stream.lost
    assert len(fake.writes) == 3


def test_settled_nonempty_delivery_then_close_remains_clean(prepared):
    conn, session, fake = ready(prepared)
    fake.enqueue(node(777))
    eventually(lambda: 777 in session.backend.nodes and conn._managed_stream.rx_batch is None)
    conn.disconnect()
    conn._managed_stream.reader_thread.join(3)
    state = session.export_state()
    assert not state.wire.resync_required and not conn._managed_stream.lost
    assert state.wire.issued == 2 and len(fake.writes) == 3


def test_delivery_control_releases_token_preserves_primary_and_loss(prepared, monkeypatch):
    conn, session, fake = ready(prepared)
    primary = BaseException('inert delivery control')
    seen = []
    def controlled(*args):
        assert conn._managed_stream.rx_batch is not None
        raise primary
    monkeypatch.setattr(session, '_receive', controlled)
    monkeypatch.setattr(threading, 'excepthook', lambda args: seen.append(args.exc_value))
    fake.enqueue(node(777))
    eventually(lambda: bool(seen))
    stream = conn._managed_stream
    stream.reader_thread.join(3)
    assert seen == [primary] and not stream.reader_thread.is_alive()
    assert stream.rx_batch is None and stream.lost
    assert session.export_state().wire.resync_required


def test_reentrant_delivery_refusal_cannot_release_original_token(prepared, monkeypatch):
    conn, session, fake = ready(prepared)
    original = session._receive
    seen = []
    def reentrant(*args):
        stream = conn._managed_stream
        token = stream.rx_batch
        assert token is not None
        with pytest.raises(RuntimeError, match='batch already owned'):
            conn._deliver_managed_batch(stream, stream.serial_handle, stream.serial_incarnation, b'x')
        assert stream.rx_batch is token
        seen.append(token)
        return original(*args)
    monkeypatch.setattr(session, '_receive', reentrant)
    fake.enqueue(node(777))
    eventually(lambda: 777 in session.backend.nodes and conn._managed_stream.rx_batch is None)
    conn.disconnect()
    conn._managed_stream.reader_thread.join(3)
    assert seen and not session.export_state().wire.resync_required
