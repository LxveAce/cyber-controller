"""Successors to the preserved independent15a RX-retention witness.

The historical second-feed non-overflow precondition changes intentionally: its admission must fail
when the full first immutable chunk still occupies the64KiB queue budget. Parser slices are separate.
"""
from __future__ import annotations

import threading

import pytest

from src.protocols import meshtastic_proto as mp
from src.protocols.meshtastic_stream import ConfigLimits, MeshtasticBackend
from src.protocols.stream_framer import StreamFramer


def retained(backend):
    return len(backend._rx_current) + sum(map(len, backend._rx))


def status(backend):
    return backend.snapshot()['config_status']


@pytest.mark.parametrize('extra', [1, 4096])
def test_default_retained_prefix_keeps_capacity_charged_and_rejects_second_feed(extra):
    backend = MeshtasticBackend(lambda _: None)
    backend.start()
    backend.feed_bytes(b'\x94\xc3\x00\x00' * (65536 // 4))
    assert backend._rx_offset == 4096 and len(backend._rx_current) == 65536
    assert retained(backend) == status(backend)['queued_rx_bytes'] == 65536
    backend.feed_bytes(b'x' * extra)
    current = status(backend)
    assert current['resync_required'] and current['reason'] == 'capacity'
    assert retained(backend) == current['queued_rx_bytes'] == 0
    assert not backend.config_complete and not backend.request_config().accepted


def test_second_chunk_uses_only_real_spare_retained_capacity():
    backend = MeshtasticBackend(lambda _: None)
    backend.feed_bytes(b'\x94\xc3\x00\x00' * (61440 // 4))
    backend.feed_bytes(b'x' * 4096)
    assert not status(backend)['resync_required']
    assert retained(backend) == status(backend)['queued_rx_bytes'] == 65536
    backend.feed_bytes(b'x')
    assert status(backend)['resync_required'] and retained(backend) == 0


def test_completed_current_chunk_releases_full_charge_for_later_input():
    backend = MeshtasticBackend(lambda _: None,
        limits=ConfigLimits(parser_slice=4, pump_actions=2, rx_bytes=8))
    for _ in range(3):
        backend.feed_bytes(b'\x94\xc3\x00\x00' * 2)
        assert backend._rx_offset == 4 and retained(backend) == status(backend)['queued_rx_bytes'] == 8
        backend.tick()
        assert retained(backend) == status(backend)['queued_rx_bytes'] == 0
        assert not status(backend)['resync_required']


def test_cancelled_burst_can_drain_and_release_storage_without_new_write():
    writes = []
    backend = MeshtasticBackend(writes.append)
    attempt = backend.start()
    assert backend.cancel_config(attempt.attempt_id)
    backend.feed_bytes(b'\x94\xc3\x00\x00' * 4
                       + StreamFramer.frame(mp.field_varint(7, attempt.request_id)))
    assert retained(backend) == status(backend)['queued_rx_bytes'] == 0
    assert len(writes) == 1 and not status(backend)['resync_required']
    assert backend.request_config().accepted and len(writes) == 2


def test_retirement_releases_retained_chunk_and_preserves_lost_input_truth():
    backend = MeshtasticBackend(lambda _: None)
    backend.feed_bytes(b'\x94\xc3\x00\x00' * (65536 // 4))
    assert retained(backend) == 65536
    backend.retire()
    assert retained(backend) == status(backend)['queued_rx_bytes'] == 0
    assert backend.export_wire_state().resync_required and not status(backend)['cleanup_pending']


def test_capacity_during_active_slice_uses_retained_charge_and_owner_only_reset(monkeypatch):
    backend = MeshtasticBackend(lambda _: None)
    entered, release = threading.Event(), threading.Event()
    errors, resets, feed_threads = [], [], []
    extract, reset = backend._framer._extract_one, backend._framer.reset
    first = True

    def gated_extract():
        nonlocal first
        if first:
            first = False
            assert backend._framer.buffered == 4096
            entered.set()
            assert release.wait(3)
        return extract()

    def recorded_reset():
        resets.append(threading.get_ident())
        reset()

    def feed():
        feed_threads.append(threading.get_ident())
        try:
            backend.feed_bytes(b'\x94\xc3\x00\x00' * (65536 // 4))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(backend._framer, '_extract_one', gated_extract)
    monkeypatch.setattr(backend._framer, 'reset', recorded_reset)
    worker = threading.Thread(target=feed)
    worker.start()
    try:
        assert entered.wait(3)
        backend.feed_bytes(b'x' * 4096)
        assert status(backend)['resync_required'] and retained(backend) == 0
        assert resets == [] and backend._framer.buffered == 4096
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not errors
    assert resets == feed_threads and backend._framer.buffered == 0
    assert not status(backend)['cleanup_pending']
