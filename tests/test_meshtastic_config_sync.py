"""Bounded config transactions on real protobuf/framer paths and inert callbacks only.

Case numbers refer to the frozen Mesh config transaction review plan. No transport is opened.
"""
from __future__ import annotations

import json
import threading
from dataclasses import replace

import pytest

from src.protocols import meshtastic_proto as mp
from src.protocols import meshtastic_stream as ms
from src.protocols.stream_framer import StreamFramer


def frame(field, payload):
    return StreamFramer.frame(mp.field_bytes(field, payload))


def node(num=100, name='Inert'):
    return frame(4, mp.field_varint(1, num) + mp.field_bytes(2, mp.field_bytes(2, name.encode())))


def channel(index=0):
    return frame(10, mp.field_varint(1, index & 0xFFFFFFFFFFFFFFFF) + mp.field_varint(3, 1))


def local(num=100):
    return frame(3, mp.field_varint(1, num))


def complete(number):
    return StreamFramer.frame(mp.field_varint(7, number))


def text(message='Inert'):
    return frame(2, mp.field_fixed32(1, 200) + mp.field_bytes(4,
                 mp.field_varint(1, 1) + mp.field_bytes(2, message.encode())))


def lora():
    return frame(5, mp.field_bytes(6, mp.field_varint(7, 1)))


def want_id(data):
    return mp.parse(StreamFramer().feed(data)[0])[3][0]


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


def make(**kwargs):
    writes, events = [], []
    backend = ms.MeshtasticBackend(writes.append, on_event=lambda *args: events.append(args), **kwargs)
    return backend, writes, events


def sync(backend, writes, rows=b''):
    backend.request_config()
    number = want_id(writes[-1])
    backend.feed_bytes(local() + rows + complete(number))
    assert backend.config_complete
    return number


def status(backend):
    return backend.snapshot()['config_status']


def drain(backend, turns=32):
    for _ in range(turns):
        backend.tick()


def test_c01_unrelated_marker_cannot_complete_original_witness():
    b, _, events = make(config_id=123)
    b.start()
    b.feed_bytes(complete(124))
    assert not b.config_complete
    assert not any(kind == 'mesh_config_complete' for kind, _ in events)


def test_c02_c06_c07_refresh_replaces_frozen_inventory_and_config():
    b, writes, events = make(config_id=123)
    first = sync(b, writes, node(100) + node(200) + channel(0) + channel(1) + lora())
    before = b.snapshot()
    b.request_config()
    second = want_id(writes[-1])
    assert second != first
    b.feed_bytes(local() + node(100, 'Changed') + channel(0) + complete(first))
    pending = b.snapshot()
    assert not pending['config_complete'] and pending['config_status']['inventory_stale']
    for key in ('nodes', 'channels', 'my_node_num', 'lora_config'):
        assert pending[key] == before[key]
    assert b.node_list()[0].long_name == 'Inert' and len(b.active_channels()) == 2
    pending['nodes'][0]['long_name'] = 'External mutation'
    pending['config_status']['state'] = 'ready'
    b.feed_bytes(complete(second))
    assert [row.num for row in b.node_list()] == [100], 'Absent old node survived a completed refresh'
    assert [row.index for row in b.active_channels()] == [0], 'Absent old channel survived a completed refresh'
    assert b.lora_config is None and b.nodes[100].long_name == 'Changed'
    assert status(b)['inventory_revision'] == before['config_status']['inventory_revision'] + 1
    assert [kind for kind, _ in events].count('mesh_node') == 0
    assert [kind for kind, _ in events].count('mesh_config_complete') == 2


def test_c03_unsolicited_and_duplicate_markers_do_not_change_readiness_or_revision():
    b, writes, _ = make()
    b.feed_bytes(node() + complete(123))
    assert not b.config_complete
    number = sync(b, writes, node())
    revision = status(b)['inventory_revision']
    b.feed_bytes(complete(number) + complete(number + 1))
    assert b.config_complete and status(b)['inventory_revision'] == revision


@pytest.mark.parametrize('marker', [b'', local(mp.BROADCAST_NUM)])
def test_c04_completion_needs_current_attempt_local_identity(marker):
    b, writes, _ = make()
    sync(b, writes, node())
    b.request_config()
    b.feed_bytes(marker + complete(want_id(writes[-1])))
    expected = 'source_changed' if marker else 'missing_local'
    assert status(b)['reason'] == expected and not b.config_complete
    assert [row.num for row in b.node_list()] == [100]


def test_c04_local_identity_alone_is_sufficient_without_local_node_row():
    b, writes, _ = make()
    sync(b, writes)
    assert b.my_node_num == 100 and b.node_list() == []


@pytest.mark.parametrize('rows', [local() + node(), node() + local()])
def test_c05_pending_local_marker_is_order_independent(rows):
    b, writes, _ = make()
    b.start()
    b.feed_bytes(rows + complete(want_id(writes[-1])))
    assert b.config_complete and b.node_list()[0].is_local


def test_c08_concurrent_requests_coalesce_while_writer_owns_call():
    entered, release = threading.Event(), threading.Event()
    writes, errors, initial = [], [], []

    def writer(data):
        writes.append(data)
        entered.set()
        assert release.wait(3)

    b = ms.MeshtasticBackend(writer)

    def start():
        try:
            initial.append(b.start())
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=start)
    worker.start()
    try:
        assert entered.wait(3)
        again = b.request_config()
        assert again.accepted and again.reason == 'coalesced' and len(writes) == 1
        assert status(b)['cleanup_pending']
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not errors
    assert initial[0].attempt_id == again.attempt_id


@pytest.mark.parametrize('seed', [0, -1, 2**32, True, 1.0, '1'])
def test_c09_invalid_request_seed_is_not_coerced(seed):
    with pytest.raises((TypeError, ValueError)):
        make(config_id=seed)


@pytest.mark.parametrize('seed, expected', [(69419, [69419, 69422]), (0xFFFFFFFF, [0xFFFFFFFF, 1])])
def test_c09_allocator_skips_reserved_ids_and_wraps_without_reuse(seed, expected):
    b, writes, _ = make(config_id=seed)
    assert [sync(b, writes), sync(b, writes)] == expected
    b.retire()
    state = b.export_wire_state()
    # Boundary injection represents an exhausted real uint32 epoch without billions of writes.
    state = replace(state, issued=0xFFFFFFFD)
    exhausted = ms.MeshtasticBackend(lambda _: pytest.fail('Reused exhausted ID'), wire_state=state)
    assert exhausted.start().reason == 'id_exhausted'


def test_c10_c11_c12_c13_deadline_truth_tick_and_drain():
    clock = Clock()
    b, writes, events = make(monotonic=clock)
    sync(b, writes, node())
    attempt = b.request_config()
    b.feed_bytes(local() + node(200))
    clock.value = 15.0
    count = len(events)
    assert status(b)['reason'] == 'timeout' and not b.config_complete
    assert len(events) == count and len(writes) == 2
    b.tick()
    timeouts = [data for kind, data in events if kind == 'mesh_config_status' and data['reason'] == 'timeout']
    b.tick()
    assert len(timeouts) == 1 and len(events) == count + 1
    assert [row.num for row in b.node_list()] == [100]
    assert b.request_config().reason == 'busy_draining'
    b.feed_bytes(complete(attempt.request_id + 50))
    assert not b.request_config().accepted
    b.feed_bytes(node(300) + complete(attempt.request_id))
    assert not b.config_complete and [row.num for row in b.node_list()] == [100]
    assert b.request_config().request_id != attempt.request_id
    assert len(writes) == 3


def test_c10_progress_does_not_extend_deadline_and_late_completion_without_tick_fails():
    clock = Clock()
    b, writes, _ = make(monotonic=clock)
    b.start()
    clock.value = 14
    b.feed_bytes(local())
    clock.value = 15
    b.feed_bytes(complete(want_id(writes[-1])))
    assert not b.config_complete and status(b)['reason'] == 'timeout'
    assert b.request_config().accepted  # the late marker only drained its old owner


def test_c10_writer_time_is_included():
    clock = Clock()
    b = None

    def writer(data):
        b.feed_bytes(local() + complete(want_id(data)))
        clock.value = 15

    b = ms.MeshtasticBackend(writer, monotonic=clock)
    b.start()
    assert not b.config_complete and status(b)['reason'] == 'timeout'


def test_c14_prewrite_cancel_is_definitely_not_written_and_token_scoped():
    writes, results = [], []
    b = None

    def sink(kind, data):
        if kind == 'mesh_text':
            attempt = b.request_config()
            results.append(b.cancel_config(attempt.attempt_id))

    b = ms.MeshtasticBackend(writes.append, on_event=sink)
    b.feed_bytes(text())
    assert writes == [] and results == [True] and status(b)['reason'] == 'cancelled'
    newer = b.request_config()
    assert newer.accepted and len(writes) == 1
    assert not b.cancel_config(newer.attempt_id - 1)


def test_c14_c16_writer_reentry_can_cancel_without_replacement_write():
    writes, calls = [], []
    b = None

    def writer(data):
        writes.append(data)
        request = b.request_config()
        calls.append(b.snapshot())
        assert request.reason == 'coalesced'
        assert b.cancel_config(request.attempt_id)
        assert b.request_config().reason == 'busy_draining'
        b.feed_bytes(local() + complete(want_id(data)))
        b.tick()

    b = ms.MeshtasticBackend(writer)
    b.start()
    assert len(writes) == len(calls) == 1 and not b.config_complete
    assert status(b)['reason'] == 'cancelled' and status(b)['phase'] is None


@pytest.mark.parametrize('error', [None, OSError('exact inert writer failure')])
def test_c15_sync_response_commits_only_after_successful_writer_return(error):
    observations = []
    b = None

    def writer(data):
        b.feed_bytes(local() + node() + complete(want_id(data)))
        observations.append(b.config_complete)
        if error is not None:
            raise error

    b = ms.MeshtasticBackend(writer)
    if error is None:
        b.start()
        assert b.config_complete
    else:
        with pytest.raises(OSError) as raised:
            b.start()
        assert raised.value is error and not b.config_complete
        assert status(b)['reason'] == 'write_failed'
        drain(b)
        assert not b.config_complete and b.node_list() == []
    assert observations == [False]


def test_c17_completion_callback_cannot_put_same_batch_tail_in_next_attempt():
    writes, requests = [], []
    b = None

    def sink(kind, data):
        if kind == 'mesh_config_complete':
            requests.append(b.request_config())

    b = ms.MeshtasticBackend(writes.append, on_event=sink)
    b.start()
    b.feed_bytes(local() + node() + complete(want_id(writes[-1])) + node(200))
    assert len(writes) == 2 and len(requests) == 1
    assert [row.num for row in b.node_list()] == [100, 200]
    assert status(b)['pending_nodes'] == 0
    b.feed_bytes(local() + complete(want_id(writes[-1])))
    assert b.node_list() == []


@pytest.mark.parametrize('kind', ['event', 'debug'])
def test_c18_c19_reentrant_subscriber_retire_fences_second_effect(kind):
    calls = []
    b = None

    def sink(*args):
        calls.append(args)
        b.snapshot()
        b.retire()

    b = ms.MeshtasticBackend(lambda _: None, **({'on_event': sink} if kind == 'event' else {'on_text': sink}))
    b.feed_bytes(text('first') + text('second') if kind == 'event' else b'first\nsecond\n')
    assert len(calls) == 1 and b.snapshot()['retired'] and not status(b)['cleanup_pending']


@pytest.mark.parametrize('kind', ['event', 'debug'])
def test_c19_ordinary_subscriber_errors_isolate_after_framing(kind):
    calls = []
    b = None

    def sink(*args):
        calls.append(args)
        assert not b._framer.buffered
        raise RuntimeError('isolated ordinary subscriber')

    b = ms.MeshtasticBackend(lambda _: None, **({'on_event': sink} if kind == 'event' else {'on_text': sink}))
    b.feed_bytes(text('first') + text('second') if kind == 'event' else b'first\nsecond\n')
    assert len(calls) == 2 and not b.snapshot()['retired'] and not status(b)['cleanup_pending']


@pytest.mark.parametrize('control_type', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('seam', ['writer', 'clock', 'event', 'debug'])
def test_c20_original_control_fences_queued_effects_and_releases_lease(control_type, seam):
    control = control_type('exact original control')
    calls = []
    b = None

    def fail(*args):
        calls.append(args)
        raise control

    def writer(data):
        b.feed_bytes(local() + node() + complete(want_id(data)))
        raise control

    options = {'on_event': fail} if seam == 'event' else {'on_text': fail} if seam == 'debug' else {}
    b = ms.MeshtasticBackend(writer if seam == 'writer' else lambda _: None, **options)
    if seam == 'clock':
        b._clock = fail
    with pytest.raises(control_type) as raised:
        if seam == 'writer':
            b.start()
        elif seam == 'clock':
            b.snapshot()
        else:
            b.feed_bytes(text('first') + text('second') if seam == 'event' else b'first\nsecond\n')
    assert raised.value is control
    b._clock = Clock()
    assert b.snapshot()['retired'] and not b.config_complete and not status(b)['cleanup_pending']
    assert status(b)['queued_events'] == 0 and b.node_list() == []
    if seam in ('event', 'debug'):
        assert len(calls) == 1
    b.tick()
    assert not b.start().accepted and b._framer.buffered == 0


def test_c20_control_after_commit_keeps_preceding_snapshot_stale():
    control = KeyboardInterrupt('completion subscriber')

    def sink(kind, data):
        if kind == 'mesh_config_complete':
            raise control

    writes = []
    b = ms.MeshtasticBackend(writes.append, on_event=sink)
    b.start()
    with pytest.raises(KeyboardInterrupt) as raised:
        b.feed_bytes(local() + node() + complete(want_id(writes[-1])) + text())
    assert raised.value is control and b.snapshot()['retired'] and not b.config_complete
    assert [row.num for row in b.node_list()] == [100] and status(b)['inventory_confirmed']
    assert status(b)['queued_events'] == 0


def test_c21_exact_512_node_boundary_and_duplicate_charging():
    b, writes, _ = make()
    b.start()
    for number in range(100, 612):
        b.feed_bytes(node(number))
    assert status(b)['pending_nodes'] == 512
    frames = status(b)['input_frames']
    b.feed_bytes(node(100, 'Replacement'))
    assert status(b)['pending_nodes'] == 512 and status(b)['input_frames'] == frames + 1
    b.feed_bytes(local() + complete(want_id(writes[-1])))
    assert b.config_complete and len(b.node_list()) == 512
    b.request_config()
    for number in range(100, 613):
        b.feed_bytes(node(number))
    assert status(b)['reason'] == 'capacity' and len(b.node_list()) == 512
    assert b.request_config().reason == 'busy_draining'


@pytest.mark.parametrize('extra', [None, -1, 8])
def test_c22_exact_channel_slot_boundary(extra):
    b, writes, _ = make()
    b.start()
    b.feed_bytes(local() + b''.join(channel(i) for i in range(8)))
    assert status(b)['pending_channels'] == 8
    if extra is not None:
        b.feed_bytes(channel(extra))
    b.feed_bytes(complete(want_id(writes[-1])))
    assert b.config_complete is (extra is None)
    assert len(b.channels) == (8 if extra is None else 0)


@pytest.mark.parametrize('extra', [0, 1])
def test_c23_exact_projected_content_boundary_preserves_prior_view(extra):
    probe, _, _ = make()
    probe.feed_bytes(local() + node(name='x' * 20))
    cap = status(probe)['inventory_bytes']
    view = probe.snapshot()
    content = {key: view[key] for key in ('nodes', 'channels', 'my_node_num', 'lora_config')}
    assert cap == len(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
    b, writes, _ = make(limits=ms.ConfigLimits(inventory_bytes=cap))
    sync(b, writes, node(name='x'))
    b.request_config()
    b.feed_bytes(local() + node(name='x' * (20 + extra)))
    b.feed_bytes(complete(want_id(writes[-1])))
    assert b.config_complete is (extra == 0)
    assert b.nodes[100].long_name == ('x' * 20 if extra == 0 else 'x')
    if extra == 0:
        b.feed_bytes(node(name='x'))
        assert status(b)['inventory_bytes'] == cap - 19


@pytest.mark.parametrize('boundary', ['frames', 'bytes', 'debug'])
def test_c24_unknown_duplicate_and_debug_input_are_charged(boundary):
    unknown = StreamFramer.frame(mp.field_varint(99, 1))
    options = {'input_frames': 2} if boundary == 'frames' else {'input_bytes': len(unknown) * 2}
    b, _, _ = make(limits=ms.ConfigLimits(**options))
    b.start()
    b.feed_bytes(unknown * 2)
    assert status(b)['state'] == 'syncing' and status(b)['input_frames'] == 2
    b.feed_bytes(b'x' if boundary == 'debug' else unknown)
    assert status(b)['reason'] == 'capacity' and not b.config_complete


@pytest.mark.parametrize('boundary', ['chunks', 'bytes', 'single'])
def test_c25_receive_overflow_requires_clean_stream_even_after_matching_drain(boundary):
    b = None

    def writer(data):
        if boundary == 'chunks':
            for _ in range(3):
                b.feed_bytes(b'x')
        elif boundary == 'bytes':
            b.feed_bytes(b'x' * 8)
            b.feed_bytes(b'x')
        else:
            b.feed_bytes(b'x' * 9)

    b = ms.MeshtasticBackend(writer, limits=ms.ConfigLimits(rx_chunks=2, rx_bytes=8))
    attempt = b.start()
    assert status(b)['reason'] == 'capacity' and status(b)['resync_required']
    b.feed_bytes(complete(attempt.request_id))
    assert not b.request_config().accepted and not b.config_complete
    assert status(b)['queued_rx_bytes'] <= 8


def test_c26_callback_chain_stops_at_budget_and_resumes_on_tick():
    calls = []
    b = None

    def sink(kind, data):
        if kind == 'mesh_text':
            calls.append(data)
            b.feed_bytes(text())

    b = ms.MeshtasticBackend(lambda _: None, on_event=sink, limits=ms.ConfigLimits(pump_callbacks=3))
    b.feed_bytes(text())
    assert len(calls) == 3 and status(b)['queued_events'] <= 1
    b.tick()
    assert len(calls) == 6
    b.retire()
    assert not status(b)['cleanup_pending']


@pytest.mark.parametrize('payload', [bytes.fromhex('2001'), bytes.fromhex('2203086412')])
def test_c27_malformed_known_frames_fail_pending_but_valid_unknown_skips(payload):
    b, writes, _ = make()
    unknown = mp.decode_fromradio(mp.field_varint(99, 1))
    assert unknown.kind == 'other' and not unknown.malformed
    malformed = mp.decode_fromradio(payload)
    assert malformed.kind == 'other' and malformed.malformed
    b.start()
    b.feed_bytes(local() + StreamFramer.frame(mp.field_varint(99, 1)))
    assert status(b)['state'] == 'syncing'
    b.feed_bytes(StreamFramer.frame(payload) + complete(want_id(writes[-1])))
    assert status(b)['reason'] == 'malformed' and not b.config_complete


@pytest.mark.parametrize('prefix', [b'INFO boot\n', b'\x94\xc3\x02\x01'])
def test_c28_invalid_declared_length_aborts_slice_but_debug_does_not(prefix):
    b, writes, _ = make()
    b.start()
    b.feed_bytes(prefix + local() + complete(want_id(writes[-1])))
    assert b.config_complete is prefix.startswith(b'INFO')
    if not b.config_complete:
        assert status(b)['reason'] == 'malformed'


def test_c28_partial_frame_is_bounded_and_expires_without_completion():
    clock = Clock()
    b, _, _ = make(monotonic=clock)
    b.start()
    b.feed_bytes(b'\x94\xc3\x02\x00' + b'x' * 500)
    assert b._framer.buffered == 504
    clock.value = 15
    b.tick()
    assert status(b)['reason'] == 'timeout' and not b.config_complete


def test_c29_unconfirmed_and_later_observations_have_detached_provenance():
    b, writes, events = make()
    b.feed_bytes(node())
    observed = events[-1][1]
    assert observed['session_id'] == b.session_id and not observed['inventory_confirmed']
    sync(b, writes, node())
    revision = status(b)['inventory_revision']
    b.feed_bytes(node(200))
    assert status(b)['inventory_revision'] == revision + 1 and status(b)['changed_since_confirmation']
    assert events[-1][1]['inventory_confirmed']


def test_c30_text_emits_once_during_collecting_and_draining():
    b, _, events = make()
    attempt = b.start()
    b.feed_bytes(text('collecting'))
    b.cancel_config(attempt.attempt_id)
    b.feed_bytes(text('draining') + complete(attempt.request_id))
    assert [data['text'] for kind, data in events if kind == 'mesh_text'] == ['collecting', 'draining']
    assert b.node_list() == [] and not b.config_complete


@pytest.mark.parametrize('number', [200, mp.BROADCAST_NUM])
def test_c31_confirmed_local_identity_change_retires_without_rotating_token(number):
    b, writes, _ = make()
    sync(b, writes, node())
    token = b.session_id
    b.request_config()
    b.feed_bytes(local(number) + node(300) + complete(want_id(writes[-1])))
    assert b.snapshot()['retired'] and status(b)['reason'] == 'source_changed'
    assert b.session_id == token and [row.num for row in b.node_list()] == [100]


def test_c32_concurrent_retire_defers_real_framer_reset_to_feed_lease_owner(monkeypatch):
    b, writes, _ = make()
    # Keep a genuine unterminated debug tail before gating the actual framer buffer mutation.
    b._on_text = lambda _: None
    b.feed_bytes(b'old debug tail')
    b.start()
    entered, release = threading.Event(), threading.Event()
    errors, resets, feed_threads = [], [], []
    extract, reset = b._framer._extract_one, b._framer.reset

    def gated_extract():
        assert b._framer.buffered > 0
        entered.set()
        assert release.wait(3)
        return extract()

    first = True

    def extract_once():
        nonlocal first
        if first:
            first = False
            return gated_extract()
        return extract()

    def recorded_reset():
        resets.append((threading.get_ident(), entered.is_set(), release.is_set()))
        reset()

    def feed():
        feed_threads.append(threading.get_ident())
        try:
            b.feed_bytes(local() + node() + complete(want_id(writes[-1])))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(b._framer, '_extract_one', extract_once)
    monkeypatch.setattr(b._framer, 'reset', recorded_reset)
    worker = threading.Thread(target=feed)
    worker.start()
    try:
        assert entered.wait(3)
        buffered, debug = b._framer.buffered, bytes(b._text_buf)
        b.retire()
        assert b.snapshot()['retired'] and status(b)['cleanup_pending']
        assert not b.request_config().accepted and len(writes) == 1
        assert resets == [] and b._framer.buffered == buffered and bytes(b._text_buf) == debug
        with pytest.raises(RuntimeError):
            b.export_wire_state()
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not errors
    assert resets == [(feed_threads[0], True, True)]
    assert not status(b)['cleanup_pending'] and b._framer.buffered == 0 and b._text_buf == b''
    assert b.node_list() == [] and not b.config_complete and b.export_wire_state().resync_required


def test_c32_retire_inside_writer_never_commits_queued_dump():
    b = None

    def writer(data):
        b.feed_bytes(local() + complete(want_id(data)))
        b.retire()
        assert status(b)['cleanup_pending']
        assert not b.start().accepted

    b = ms.MeshtasticBackend(writer)
    b.start()
    assert b.snapshot()['retired'] and not b.config_complete and not status(b)['cleanup_pending']


def test_c34_adapted_ordinary_read_then_original_text_writer_error():
    error = OSError('Inert write failure')
    writes, events = [], []

    def writer(data):
        if 3 in mp.parse(StreamFramer().feed(data)[0]):
            writes.append(data)
        else:
            raise error

    b = ms.MeshtasticBackend(writer, on_event=lambda *args: events.append(args), config_id=123)
    b.start()
    b.feed_bytes(local() + node() + channel() + complete(want_id(writes[-1])) + text('Inert old text'))
    assert b.config_complete and b.node_list()[0].is_local
    received = [data for kind, data in events if kind == 'mesh_text']
    assert received[-1]['text'] == 'Inert old text'
    with pytest.raises(OSError) as raised:
        b.send_text('Inert')
    assert raised.value is error


def test_c35_same_stream_transfer_retains_drain_and_allocator_history():
    old, old_writes, _ = make(config_id=123)
    old.start()
    old_id = want_id(old_writes[-1])
    old.retire()
    state = old.export_wire_state()
    assert state.outstanding_id == old_id and not state.resync_required
    writes = []
    current = ms.MeshtasticBackend(writes.append, wire_state=state)
    assert current.session_id != old.session_id
    assert not current.start().accepted and writes == []
    current.feed_bytes(node(200) + complete(old_id + 7))
    assert not current.start().accepted and current.node_list() == []
    current.feed_bytes(complete(old_id))
    attempt = current.start()
    assert attempt.accepted and want_id(writes[-1]) != old_id
    current.feed_bytes(local() + complete(attempt.request_id))
    assert current.config_complete and current.node_list() == []
    current.retire()
    next_state = current.export_wire_state()
    assert next_state.epoch == state.epoch and next_state.issued == 2 and next_state.outstanding_id is None


def test_c04_initial_sentinel_local_identity_fails_missing_local():
    b, writes, _ = make()
    b.start()
    b.feed_bytes(local(mp.BROADCAST_NUM) + complete(want_id(writes[-1])))
    assert status(b)['reason'] == 'missing_local' and not b.config_complete
    assert not b.snapshot()['retired']


def test_c10_queued_attempt_can_expire_before_any_writer_admission():
    writes, events = [], []
    clock = Clock()
    b = None

    def sink(kind, data):
        events.append((kind, data))
        if kind == 'mesh_text':
            b.request_config()
            clock.value = 15

    b = ms.MeshtasticBackend(writes.append, on_event=sink, monotonic=clock)
    b.feed_bytes(text())
    assert writes == [] and status(b)['reason'] == 'timeout' and status(b)['phase'] is None
    assert len([data for kind, data in events if data.get('reason') == 'timeout']) == 1
    assert b.request_config().accepted and len(writes) == 1


def test_c11_rejected_mutating_request_settles_timeout_notification_once():
    clock = Clock()
    b, _, events = make(monotonic=clock)
    b.start()
    clock.value = 15
    assert not b.request_config().accepted
    assert not b.request_config().accepted
    assert len([data for kind, data in events if data.get('reason') == 'timeout']) == 1


@pytest.mark.parametrize('boundary', ['count', 'bytes', 'completion'])
def test_c25_notification_loss_cannot_publish_false_complete_inventory(boundary):
    options = {'events': 1} if boundary != 'bytes' else {'event_bytes': 1}
    b, writes, _ = make(limits=ms.ConfigLimits(**options))
    if boundary != 'bytes':
        b.feed_bytes(node())
    b.start()
    stream = local() + node(200) + text('first')
    stream += complete(want_id(writes[-1])) if boundary == 'completion' else text('second')
    b.feed_bytes(stream)
    assert status(b)['reason'] == 'capacity' and status(b)['resync_required']
    assert not b.config_complete and status(b)['queued_events'] == 0
    assert [row.num for row in b.node_list()] == ([100] if boundary != 'bytes' else [])


def test_c20_clock_cleanup_error_does_not_replace_original_control(monkeypatch):
    original, cleanup = KeyboardInterrupt('clock original'), SystemExit('cleanup secondary')
    b, _, _ = make()

    def clock():
        raise original

    def reset():
        raise cleanup

    b._clock = clock
    monkeypatch.setattr(b._framer, 'reset', reset)
    with pytest.raises(KeyboardInterrupt) as raised:
        b.snapshot()
    assert raised.value is original and b._pump_owner is None and b._retired


def test_c26_action_budget_leaves_bounded_work_for_tick():
    b = ms.MeshtasticBackend(lambda _: None, limits=ms.ConfigLimits(pump_actions=2))
    b.feed_bytes(node(100) + node(200))
    assert [row.num for row in b.node_list()] == [100]
    b.tick()
    assert [row.num for row in b.node_list()] == [100, 200]


def test_c29_revision_exhaustion_fences_instead_of_reusing_identity():
    b, _, _ = make()
    b._revision = 0xFFFFFFFFFFFFFFFF
    b.feed_bytes(node())
    assert b.snapshot()['retired'] and status(b)['reason'] == 'revision_exhausted'
    assert b.node_list() == []


def test_c17_preceding_partial_frame_finishes_before_next_writer_admission():
    writes = []
    b = None

    def sink(kind, data):
        if kind == 'mesh_config_complete':
            b.request_config()

    b = ms.MeshtasticBackend(writes.append, on_event=sink)
    b.start()
    tail = node(200)
    b.feed_bytes(local() + complete(want_id(writes[-1])) + tail[:5])
    assert len(writes) == 1 and status(b)['phase'] == 'queued'
    b.feed_bytes(tail[5:])
    assert len(writes) == 2 and status(b)['pending_nodes'] == 0
    assert [row.num for row in b.node_list()] == [200]
    b.feed_bytes(local() + complete(want_id(writes[-1])))
    assert b.node_list() == []
