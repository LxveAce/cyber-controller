"""Source-only managed provider: real serial lifecycle/reader, inert fake adapter, raw Mesh frames."""
from __future__ import annotations

import threading
import time
from collections import deque

import pytest

from src.core import serial_handler as sh
from src.protocols import meshtastic_proto as mp
from src.protocols.stream_framer import StreamFramer


def complete(number):
    return StreamFramer.frame(mp.field_varint(7, number))


def local(number=100):
    return StreamFramer.frame(mp.field_bytes(3, mp.field_varint(1, number)))


def node(number):
    return StreamFramer.frame(mp.field_bytes(4, mp.field_varint(1, number)))


def decoded_write(frame):
    fields = mp.parse(StreamFramer().feed(frame)[0])
    return fields[3][0] if 3 in fields else None


def eventually(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, "fixture condition did not settle"
        time.sleep(0.002)


class FakeSerial:
    def __init__(self, *, respond=True):
        self.is_open = False
        self.opened = self.flushes = 0
        self.writes, self.read_sizes = [], []
        self.responses = deque()
        self.condition = threading.Condition()
        self.respond = respond
        self.write_hook = self.read_hook = self.flush_hook = None
        self.close_error = None

    def open(self):
        self.is_open = True
        self.opened += 1

    def close(self):
        if self.close_error is not None:
            raise self.close_error
        self.is_open = False
        with self.condition:
            self.condition.notify_all()

    def enqueue(self, chunk):
        with self.condition:
            self.responses.append(chunk)
            self.condition.notify_all()

    @property
    def in_waiting(self):
        with self.condition:
            return sum(len(chunk) for chunk in self.responses)

    def read(self, size):
        self.read_sizes.append(size)
        if self.read_hook is not None:
            return self.read_hook(size)
        with self.condition:
            if not self.responses:
                self.condition.wait(0.01)
            if not self.responses:
                return b""
            chunk = self.responses.popleft()
            if len(chunk) > size:
                self.responses.appendleft(chunk[size:])
                chunk = chunk[:size]
            return chunk

    def write(self, payload):
        self.writes.append(payload)
        if self.write_hook is not None:
            return self.write_hook(payload)
        request_id = decoded_write(payload)
        if self.respond and request_id is not None:
            count = sum(decoded_write(w) is not None for w in self.writes)
            self.enqueue((b"" if count == 1 else local()+node(100)) + complete(request_id))
        return len(payload)

    def flush(self):
        self.flushes += 1
        if self.flush_hook is not None:
            self.flush_hook()


@pytest.fixture
def prepared(monkeypatch):
    owned = []
    def make(*, respond=True, **options):
        fake = FakeSerial(respond=respond)
        monkeypatch.setattr(sh.serial, 'Serial', lambda: fake)
        conn = sh.SerialConnection('FIXTURE-MESH', timeout=0.05)
        session = conn.prepare_mesh_session(randbelow=lambda _: 120, **options)
        owned.append((conn, fake))
        return conn, session, fake
    yield make
    for conn, fake in owned:
        fake.close_error = None
        conn.disconnect()
        reader = conn._read_thread
        if reader is not None and reader.ident is not None:
            reader.join(3)
            assert not reader.is_alive(), 'fixture reader leaked'


def test_preparation_is_inert_and_reader_owns_one_d_a_b(prepared):
    conn, session, fake = prepared()
    assert not fake.writes and fake.opened == 0 and conn._read_thread is None
    assert conn.mesh_backend is session.backend and not session.snapshot()['config_complete']
    conn.connect()
    eventually(lambda: session.snapshot()['config_complete'])
    assert [decoded_write(w) for w in fake.writes] == [None, 121, 122]
    assert fake.flushes == 3 and fake.opened == 1
    assert session.backend.node_list()[0].num == 100
    conn.connect()
    session.snapshot()
    assert len(fake.writes) == 3 and fake.opened == 1


@pytest.mark.parametrize('call', [
    lambda c: c.write('legacy'), lambda c: c.write_receipt('legacy'),
    lambda c: c.write_bytes(b'legacy'), lambda c: c.write_bytes_receipt(b'legacy'),
    lambda c: c.send_interrupt(), lambda c: c.send_interrupt_receipt(),
    lambda c: c._write_payload_attempt(b'legacy'),
])
def test_every_unleased_api_refuses_before_adapter_entry(prepared, call):
    conn, session, fake = prepared()
    conn.connect()
    eventually(lambda: session.snapshot()['config_complete'])
    before = list(fake.writes)
    with pytest.raises(sh.ManagedSerialUnavailable):
        call(conn)
    assert fake.writes == before and not conn._write_quarantined


def test_wrong_lease_and_incarnation_refuse_and_cannot_rebind(prepared):
    conn, session, fake = prepared()
    conn.connect()
    eventually(lambda: session.snapshot()['config_complete'])
    lease = session._lease
    for invalid, token in ((sh.ManagedSerialLease(lease.incarnation), lease.incarnation),
                           (lease, object()), (object(), lease.incarnation)):
        with pytest.raises(sh.ManagedSerialUnavailable):
            conn.write_bound_bytes_receipt(invalid, token, b'not-entered')
    assert len(fake.writes) == 3
    with pytest.raises(RuntimeError):
        conn.prepare_mesh_session()
    conn.disconnect()
    with pytest.raises(RuntimeError):
        conn.connect()
    with pytest.raises(sh.ManagedSerialUnavailable):
        conn.write_bound_bytes_receipt(lease, lease.incarnation, b'not-entered')
    assert len(fake.writes) == 3


@pytest.mark.parametrize('timeout', [None, 0, -1, True, 1.001, float('inf'), float('nan')])
def test_unbounded_or_busy_loop_read_profile_refused_before_open(timeout):
    conn = sh.SerialConnection('FIXTURE-MESH', timeout=timeout)
    with pytest.raises(ValueError):
        conn.prepare_mesh_session()
    assert conn._managed_stream is None and conn._serial is None
    with pytest.raises(RuntimeError, match='preparation'):
        conn.connect()


def test_silent_reader_runs_deadline_maintenance(prepared):
    clock = [0.0]
    conn, session, fake = prepared(respond=False, monotonic=lambda: clock[0])
    conn.connect()
    eventually(lambda: len(fake.writes) == 2)
    clock[0] = 16.0
    eventually(lambda: session.snapshot()['config_status']['reason'] == 'timeout')
    assert len(fake.writes) == 2 and fake.read_sizes


def test_managed_batch_tail_stays_private_and_raw_flag_is_not_authority(prepared):
    conn, session, fake = prepared(respond=False)
    lines, diagnostics, events = [], [], []
    conn.on_line(lines.append)
    conn.on_bytes(diagnostics.append)
    session.subscribe(lambda *args: events.append(args))
    def reply(payload):
        number = decoded_write(payload)
        if number == 121:
            fake.enqueue(complete(number)+node(999)+b'private\n')
        elif number == 122:
            fake.enqueue(local()+node(100)+complete(number))
        return len(payload)
    fake.write_hook = reply
    conn.raw = False
    conn.connect()
    eventually(lambda: session.snapshot()['config_complete'])
    assert set(session.backend.nodes) == {100} and not lines
    assert diagnostics[0] == complete(121)+node(999)+b'private\n'
    assert not any(kind == 'mesh_log' and data['line'] == 'private' for kind, data in events)


def test_subscription_removal_preserves_owner_and_does_not_bootstrap(prepared):
    conn, session, fake = prepared()
    seen = []
    remove = session.subscribe(lambda *args: seen.append(args))
    conn.connect()
    eventually(lambda: session.snapshot()['config_complete'])
    assert seen
    remove(); remove()
    before = len(seen)
    session._publish('fixture', {})
    assert len(seen) == before and len(fake.writes) == 3
    assert conn.mesh_backend is session.backend


def test_clean_idle_close_exports_complete_history_and_reduces_trust_on_new_handle(prepared):
    conn, session, fake = prepared()
    conn.connect()
    eventually(lambda: session.snapshot()['config_complete'])
    conn.disconnect()
    state = session.export_state()
    assert state.wire.issued == 2 and not state.wire.resync_required
    newer, adopted, transport = prepared(state=state)
    assert not adopted.snapshot()['config_complete']
    newer.connect()
    eventually(lambda: len(transport.writes) >= 2)
    assert decoded_write(transport.writes[1]) == 123


def test_failed_close_retains_same_session_until_actual_handle_release(prepared):
    conn, session, fake = prepared()
    conn.connect()
    eventually(lambda: session.snapshot()['config_complete'])
    fake.close_error = RuntimeError('fixture close failure')
    conn.disconnect()
    assert conn._serial is fake and conn._managed_stream.session is session
    assert session.snapshot()['transport_status']['cleanup_pending']
    with pytest.raises(RuntimeError):
        session.export_state()
    fake.close_error = None
    conn.disconnect()
    assert conn._serial is None
    assert not session.snapshot()['transport_status']['cleanup_pending']
    assert session.export_state().wire.issued == 2


def test_prepared_reader_control_keeps_exact_primary_and_loss_state(prepared, monkeypatch):
    conn, session, fake = prepared(respond=False)
    primary = BaseException('fixture read control')
    seen = []
    monkeypatch.setattr(threading, 'excepthook', lambda args: seen.append(args.exc_value))
    def fail_read(size):
        raise primary
    fake.read_hook = fail_read
    conn.connect()
    eventually(lambda: bool(seen))
    assert seen == [primary]
    conn._read_thread.join(3)
    assert session.snapshot()['retired']
    assert session.export_state().wire.resync_required


def test_managed_read_allocation_is_capped(prepared):
    conn, session, fake = prepared(respond=False)
    fake.enqueue(b'x' * 70000)
    conn.connect()
    eventually(lambda: sum(fake.read_sizes) >= 70000)
    assert max(fake.read_sizes) == 65536


def test_public_backend_still_refuses_managed_chat(prepared):
    conn, session, fake = prepared()
    conn.connect()
    eventually(lambda: session.snapshot()['config_complete'])
    with pytest.raises(Exception):
        session.backend.send_text('not implemented in provider stage')
    assert len(fake.writes) == 3


def test_timeout_mutation_before_connect_fails_without_open(prepared):
    conn, session, fake = prepared()
    conn.timeout = None
    with pytest.raises(ValueError):
        conn.connect()
    assert fake.opened == 0 and not fake.writes and session.snapshot()['retired']
    assert session.export_state().wire.issued == 0


def test_thread_start_uncertainty_retains_prepared_owner(prepared, monkeypatch):
    conn, session, fake = prepared()
    primary = RuntimeError('fixture native start ambiguity')
    def fail_start(thread):
        raise primary
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, 'start', fail_start)
        with pytest.raises(RuntimeError) as caught:
            conn.connect()
    assert caught.value is primary and conn._managed_stream.session is session
    assert conn._managed_stream.reader_start_attempted
    assert not conn._managed_stream.reader_done.is_set()
    assert not fake.writes and not fake.is_open
    with pytest.raises(RuntimeError):
        session.export_state()
    with pytest.raises(RuntimeError):
        conn.connect()


def test_c20_bound_write_control_preserves_primary_and_fences_queued_completion(prepared):
    conn, session, fake = prepared()
    conn.connect()
    eventually(lambda: session.snapshot()['config_complete'])
    primary = BaseException('fixture entered writer control')
    def interrupt(payload):
        number = decoded_write(payload)
        session._receive(session._lease.incarnation, local(200)+node(200)+complete(number))
        raise primary
    fake.write_hook = interrupt
    with pytest.raises(BaseException) as caught:
        session.backend.request_config()
    assert caught.value is primary and conn._write_transaction is None and conn._write_quarantined
    assert session.snapshot()['retired'] and not session.snapshot()['config_complete']
    assert set(session.backend.nodes) == {100}
    assert len(fake.writes) == 4


@pytest.mark.parametrize('kind', ['zero','short','invalid','flush'])
def test_bootstrap_uses_real_receipt_quarantine_semantics(prepared, kind):
    conn, session, fake = prepared(respond=False)
    if kind == 'flush':
        def fail_flush():
            raise OSError('fixture flush failure')
        fake.flush_hook = fail_flush
    else:
        fake.write_hook = lambda data: {'zero':0,'short':1,'invalid':None}[kind]
    conn.connect()
    eventually(lambda: session.snapshot()['bootstrap_status']['phase'] == 'failed')
    assert len(fake.writes) == 1 and conn._write_quarantined
    assert not session.snapshot()['config_complete']
    assert fake.flushes == (1 if kind == 'flush' else 0)
    conn.disconnect()
    exported = session.export_state()
    assert exported.wire.resync_required is (kind != 'zero')


def test_c32_real_reader_feed_retirement_defers_framer_reset_to_reader(prepared, monkeypatch):
    conn, session, fake = prepared(respond=False)
    conn.connect()
    eventually(lambda: len(fake.writes) == 2)
    b = session.backend
    entered, release = threading.Event(), threading.Event()
    resets, errors = [], []
    extract, reset = b._framer._extract_one, b._framer.reset
    first = True
    def gated_extract():
        nonlocal first
        if first:
            first = False
            assert b._framer.buffered
            entered.set()
            assert release.wait(3)
        return extract()
    def recorded_reset():
        resets.append(threading.get_ident())
        reset()
    def close():
        try:
            conn.disconnect()
        except BaseException as exc:
            errors.append(exc)
    monkeypatch.setattr(b._framer,'_extract_one',gated_extract)
    monkeypatch.setattr(b._framer,'reset',recorded_reset)
    fake.enqueue(complete(121))
    assert entered.wait(3)
    closer = threading.Thread(target=close)
    closer.start()
    try:
        eventually(lambda: session.snapshot()['retired'])
        assert b._framer.buffered and not resets
        with pytest.raises(RuntimeError):
            session.export_state()
        assert len(fake.writes) == 2
    finally:
        release.set()
        closer.join(3)
    assert not closer.is_alive() and not errors
    assert resets == [conn._managed_stream.reader_thread.ident]
    assert session.export_state().wire.resync_required


def test_captured_old_read_cannot_adopt_current_handle(prepared):
    conn, session, fake = prepared(respond=False)
    release = threading.Event()
    entered = threading.Event()
    def blocked_read(size):
        entered.set()
        assert release.wait(3)
        return local()+node(999)+complete(121)
    fake.read_hook = blocked_read
    conn.connect()
    assert entered.wait(3)
    original = conn._serial
    replacement = FakeSerial(respond=False)
    replacement.open()
    with conn._io_lock:
        conn._serial = replacement
        conn._serial_incarnation += 1
    release.set()
    conn._read_thread.join(3)
    assert not conn._read_thread.is_alive()
    assert session.snapshot()['retired'] and not session.backend.nodes
    assert not replacement.writes
    original.close()


def test_subscriber_control_finalizes_reader_and_preserves_original(prepared, monkeypatch):
    conn, session, fake = prepared()
    primary = BaseException('fixture subscriber control')
    seen = []
    monkeypatch.setattr(threading,'excepthook',lambda args: seen.append(args.exc_value))
    def subscriber(kind, payload):
        if kind == 'mesh_config_complete':
            raise primary
    session.subscribe(subscriber)
    conn.connect()
    eventually(lambda: bool(seen))
    assert seen == [primary]
    conn._read_thread.join(3)
    assert session.snapshot()['retired'] and conn._managed_stream.reader_done.is_set()
    assert session.export_state().wire.resync_required


def test_arbitrary_connection_shape_cannot_claim_concrete_serial_profile():
    from types import SimpleNamespace

    from src.core.meshtastic_owner import MeshConnectionSession
    shaped = SimpleNamespace(write_bound_bytes_receipt=lambda *args: None)
    with pytest.raises(ValueError, match='profile_unavailable'):
        MeshConnectionSession(shaped, sh.ManagedSerialLease(object()))


def test_prelaunch_constructor_failure_is_settled_without_ambiguous_reader(prepared, monkeypatch):
    conn, session, fake = prepared()
    primary = RuntimeError('fixture constructor failure')
    def fail_constructor(*args, **kwargs):
        raise primary
    with monkeypatch.context() as patch:
        patch.setattr(threading,'Thread',fail_constructor)
        with pytest.raises(RuntimeError) as caught:
            conn.connect()
    assert caught.value is primary and fake.opened == 1 and not fake.is_open
    assert not conn._managed_stream.reader_start_attempted
    assert not session.snapshot()['transport_status']['cleanup_pending']
    assert session.export_state().wire.issued == 0


def test_managed_write_descriptor_reentry_revalidates_lease_before_entry(prepared, monkeypatch):
    conn, session, fake = prepared()
    conn.connect()
    eventually(lambda: session.snapshot()['config_complete'])
    original_getattribute = FakeSerial.__getattribute__
    armed = True
    def hooked(self, name):
        nonlocal armed
        if self is fake and name == 'write' and armed:
            armed = False
            conn._retire_managed_stream(conn._managed_stream)
        return original_getattribute(self, name)
    monkeypatch.setattr(FakeSerial,'__getattribute__',hooked)
    with pytest.raises(sh.ManagedSerialUnavailable):
        conn.write_bound_bytes_receipt(session._lease, session._lease.incarnation, b'not-entered')
    assert len(fake.writes) == 3 and conn._write_transaction is None


def test_admitted_a_batch_with_partial_tail_defers_b_until_tail_finishes(prepared):
    conn, session, fake = prepared(respond=False)
    conn.connect()
    eventually(lambda: len(fake.writes) == 2)
    tail = node(999)
    fake.enqueue(complete(121)+tail[:3])
    eventually(lambda: session.backend._framer.buffered == 3)
    assert len(fake.writes) == 2
    fake.enqueue(tail[3:])
    eventually(lambda: len(fake.writes) == 3)
    assert not session.backend.nodes
    fake.enqueue(local()+node(100)+complete(122))
    eventually(lambda: session.snapshot()['config_complete'])
    assert set(session.backend.nodes) == {100}


def test_managed_binary_reader_never_constructs_text_decoder(prepared, monkeypatch):
    conn, session, fake = prepared()
    conn.encoding = 'not-a-codec'
    conn.connect()
    eventually(lambda: session.snapshot()['config_complete'])
    assert len(fake.writes) == 3


def test_oversized_fake_read_is_not_split_to_evade_core_bound(prepared):
    conn, session, fake = prepared(respond=False)
    batches = []
    original = session._receive
    def receive(token, batch):
        batches.append(len(batch))
        original(token, batch)
    session._receive = receive
    once = True
    def read(size):
        nonlocal once
        if once:
            once = False
            return b'x' * 65537
        time.sleep(0.002)
        return b''
    fake.read_hook = read
    conn.connect()
    eventually(lambda: session.snapshot()['config_status']['reason'] == 'capacity')
    assert batches == [65537] and not session.snapshot()['config_complete']


def test_preparation_claim_excludes_connect_while_owner_is_constructing(monkeypatch):
    conn = sh.SerialConnection('FIXTURE-MESH')
    entered, release = threading.Event(), threading.Event()
    created, errors = [], []
    def random_rank(count):
        entered.set()
        assert release.wait(3)
        return 120
    def prepare():
        try:
            created.append(conn.prepare_mesh_session(randbelow=random_rank))
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=prepare)
    worker.start()
    try:
        assert entered.wait(3)
        with pytest.raises(RuntimeError, match='preparation'):
            conn.connect()
        with pytest.raises(RuntimeError):
            conn.prepare_mesh_session()
        assert conn._serial is None and conn._connect_attempt is None
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not errors and len(created) == 1
    conn.disconnect()
    assert created[0].export_state().wire.issued == 0


def test_owner_preparation_failure_cannot_fall_back_to_standalone(monkeypatch):
    conn = sh.SerialConnection('FIXTURE-MESH')
    with pytest.raises(ValueError):
        conn.prepare_mesh_session(state=object())
    assert conn._managed_preparation is not None and conn._serial is None
    with pytest.raises(RuntimeError, match='preparation'):
        conn.connect()
