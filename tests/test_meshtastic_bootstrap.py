"""Approved stage-1 B01-B22/B31: inert receipts, real wire bytes and bounded thread gates."""

from __future__ import annotations

import copy
import threading
from dataclasses import replace

import pytest

from src.core.meshtastic_owner import MeshWireOwner, OrderedSerialBinding
from src.core.serial_handler import WriteDisposition as WD
from src.core.serial_handler import WriteErrorCode as WE
from src.core.serial_handler import WriteReceipt
from src.protocols import meshtastic_proto as mp
from src.protocols import meshtastic_stream as ms
from src.protocols.stream_framer import StreamFramer


def frame(field, body):
    return StreamFramer.frame(mp.field_bytes(field, body))


def node(num=100):
    return frame(4, mp.field_varint(1, num))


def local(num=100):
    return frame(3, mp.field_varint(1, num))


def channel(index=0):
    return frame(10, mp.field_varint(1, index) + mp.field_varint(3, 1))


def complete(number):
    return StreamFramer.frame(mp.field_varint(7, number))


def text():
    return frame(
        2,
        mp.field_fixed32(1, 200)
        + mp.field_bytes(4, mp.field_varint(1, 1) + mp.field_bytes(2, b"private")),
    )


def decode_write(data):
    fields = mp.parse(StreamFramer().feed(data)[0])
    return ("config", fields[3][0]) if 3 in fields else ("disconnect", None)


def ok(data):
    return WriteReceipt(WD.HOST_WRITE_COMPLETE, len(data), len(data))


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


class Harness:
    def __init__(self, *, writer=None, **kwargs):
        self.incarnation = object()
        self.writes, self.events, self.debug = [], [], []
        self.writer = writer
        self.owner = MeshWireOwner(
            OrderedSerialBinding(self.incarnation, self.write, True, True),
            on_event=lambda *args: self.events.append(args),
            on_text=self.debug.append,
            randbelow=lambda _: 122,
            **kwargs,
        )
        self.b = self.owner.backend

    def write(self, incarnation, data):
        assert incarnation is self.incarnation
        self.writes.append(data)
        return self.writer(data) if self.writer else ok(data)

    def feed(self, data):
        self.owner.feed_bytes(self.incarnation, data)

    def begin(self):
        result = self.owner.begin_bootstrap()
        assert result.accepted
        return result

    @property
    def ids(self):
        return [number for kind, number in map(decode_write, self.writes) if kind == "config"]

    def to_b(self):
        request = self.begin()
        self.feed(complete(self.ids[0]))
        assert len(self.ids) == 2
        return request

    def ready(self):
        self.to_b()
        self.feed(local() + node() + complete(self.ids[1]))
        assert self.b.config_complete


def bs(h):
    return h.owner.snapshot()["bootstrap_status"]


def cs(h):
    return h.owner.snapshot()["config_status"]


def test_b01_b03_d_a_b_only_second_inventory_is_published():
    h = Harness()
    h.feed(node(8) + channel(7) + text() + b"private debug\n")
    h.begin()
    a = h.ids[0]
    h.feed(local(8) + node(8) + channel(7) + text())
    assert not h.b.config_complete and h.b.nodes == {} and h.b.my_node_num is None
    assert cs(h)["inventory_revision"] == 0 and not cs(h)["inventory_confirmed"]
    h.feed(complete(a) + node(9) + text())
    assert h.b.node_list() == [] and h.b.active_channels() == [] and h.debug == []
    assert not any(k != "mesh_config_status" for k, _ in h.events)
    h.feed(local() + node() + channel(0) + complete(h.ids[1]))
    assert list(h.b.nodes) == [100] and list(h.b.channels) == [0]
    assert h.b.config_complete and cs(h)["inventory_revision"] == 1
    assert [k for k, _ in h.events].count("mesh_config_complete") == 1
    assert [decode_write(w)[0] for w in h.writes] == ["disconnect", "config", "config"]
    assert bs(h)["discarded_text"] == 3


@pytest.mark.parametrize("my", [b"", local()])
def test_b02_private_terminal_needs_no_my_b_does(my):
    h = Harness()
    h.to_b()
    h.feed(my + complete(h.ids[1]))
    assert h.b.config_complete is bool(my)
    assert h.b.node_list() == []
    if not my:
        assert cs(h)["reason"] == "missing_local"


def test_b04_disconnect_resets_partial_firmware_index_before_two_full_reads():
    # Bounded source model of the pinned PhoneAPI's close/start indexing distinction. This is
    # neither firmware execution nor a claim that the actual connected device runs that release.
    class Firmware:
        index = 2

        def receive(self, data):
            kind, number = decode_write(data)
            if kind == "disconnect":
                self.index = 0
                return b""
            result = local() + b"".join(channel(i) for i in range(self.index, 3)) + complete(number)
            self.index = 0  # completed channel/config sequence advances through its reset boundary
            return result

    bare = Firmware().receive(StreamFramer.frame(mp.encode_want_config(123)))
    assert [
        mp.decode_fromradio(f).channel.index
        for f in StreamFramer().feed(bare)
        if mp.decode_fromradio(f).kind == "channel"
    ] == [2]
    firmware = Firmware()
    h = Harness(writer=lambda data: (h.feed(firmware.receive(data)), ok(data))[1])
    h.begin()
    assert h.b.config_complete and list(h.b.channels) == [0, 1, 2] and len(h.writes) == 3


def test_b05_same_parser_coalescing_and_no_ordinary_start():
    h = Harness()
    parser = h.b._framer
    assert h.b.start().reason == "bootstrap_required"
    first = h.begin()
    assert h.begin() == replace(first, reason="coalesced")
    assert not h.b.request_config().accepted
    h.feed(complete(h.ids[0]))
    assert not h.b.request_config().accepted
    assert h.begin().attempt_id == first.attempt_id
    assert h.b._framer is parser and len(h.writes) == 3


def test_b06_duplicate_reserved_wrong_markers_and_tail_are_not_inventory():
    h = Harness()
    h.begin()
    a = h.ids[0]
    h.feed(complete(0) + complete(69420) + complete(69421) + complete(a + 8))
    assert len(h.ids) == 1
    h.feed(complete(a) + complete(a) + node(7) + complete(0))
    h.feed(local() + complete(a))
    assert not h.b.config_complete and cs(h)["pending_nodes"] == 0
    h.feed(complete(h.ids[1]))
    assert h.b.config_complete


@pytest.mark.parametrize("finish", [False, True])
def test_b07_real_partial_frame_barrier_and_deadline(finish):
    clock = Clock()
    h = Harness(monotonic=clock)
    h.begin()
    tail = node(8)
    h.feed(complete(h.ids[0]) + tail[:-1])
    assert bs(h)["phase"] == "sync_barrier" and len(h.ids) == 1
    if not finish:
        clock.value = 15
        assert bs(h)["reason"] == "timeout"
    h.feed(tail[-1:])
    assert len(h.ids) == (2 if finish else 1)
    assert not h.b.config_complete and h.b.nodes == {}
    assert bs(h)["synchronized"] is finish


def test_b08_synchronous_writer_queues_whole_batch_before_b():
    def writer(data):
        kind, number = decode_write(data)
        if kind == "config" and len(h.ids) == 1:
            assert h.owner.begin_bootstrap().reason == "coalesced"
            h.feed(complete(number) + node(8))
            h.feed(node(9))  # A second batch admitted reentrantly belongs before B too.
            assert len(h.ids) == 1 and h.b.nodes == {}
        elif kind == "config":
            assert cs(h)["queued_rx_bytes"] == 0 and h.b._framer.buffered == 0
            h.feed(local() + node() + complete(number))
        return ok(data)

    h = Harness(writer=writer)
    h.begin()
    assert h.b.config_complete and list(h.b.nodes) == [100]


@pytest.mark.parametrize("phase", ["A", "B"])
@pytest.mark.parametrize("control", [False, True])
def test_b09_queued_marker_cannot_outlive_writer_exception(phase, control):
    error = KeyboardInterrupt("control") if control else RuntimeError("writer")

    def writer(data):
        kind, number = decode_write(data)
        if kind == "config":
            if len(h.ids) == (1 if phase == "A" else 2):
                h.feed(local() + node() + complete(number))
                raise error
            h.feed(complete(number))
        return ok(data)

    h = Harness(writer=writer)
    with pytest.raises(type(error)) as caught:
        h.begin()
    assert caught.value is error
    h.owner.tick()
    assert not h.b.config_complete and h.b.nodes == {} and len(h.ids) == (1 if phase == "A" else 2)
    assert not cs(h)["cleanup_pending"]


@pytest.mark.parametrize("phase", ["A", "refresh"])
def test_b10_real_framer_c32_retirement_owner(monkeypatch, phase):
    h = Harness()
    if phase == "A":
        h.begin()
    else:
        h.ready()
        h.feed(b"old debug tail")
        assert h.b._text_buf
        h.owner.request_config()
    expected_ids = len(h.ids)
    entered, release = threading.Event(), threading.Event()
    errors, resets, threads = [], [], []
    original, reset = h.b._framer._extract_one, h.b._framer.reset
    first = True

    def gated():
        nonlocal first
        if first:
            first = False
            assert h.b._framer.buffered > 0
            entered.set()
            assert release.wait(3)
        return original()

    def record_reset():
        resets.append(threading.get_ident())
        reset()

    def feed():
        threads.append(threading.get_ident())
        try:
            h.feed(complete(h.ids[-1]))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(h.b._framer, "_extract_one", gated)
    monkeypatch.setattr(h.b._framer, "reset", record_reset)
    worker = threading.Thread(target=feed)
    worker.start()
    try:
        assert entered.wait(3)
        buffered, debug = h.b._framer.buffered, bytes(h.b._text_buf)
        h.owner.retire()
        assert h.b.snapshot()["retired"] and not h.owner.begin_bootstrap().accepted
        assert h.b._framer.buffered == buffered and bytes(h.b._text_buf) == debug and not resets
        with pytest.raises(RuntimeError):
            h.owner.export_state()
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not errors and resets == threads
    assert len(h.ids) == expected_ids and h.owner.export_state().wire.resync_required


@pytest.mark.parametrize(
    "mode", ["zero", "short", "invalid", "flush", "missing", "length", "exception", "control"]
)
def test_b11_disconnect_receipt_matrix(mode):
    original = KeyboardInterrupt("D control") if mode == "control" else RuntimeError("D error")

    def writer(data):
        n = len(data)
        if mode in {"exception", "control"}:
            raise original
        return {
            "zero": lambda: WriteReceipt(WD.DEFINITELY_NOT_WRITTEN, n, 0, WE.ZERO_WRITE),
            "short": lambda: WriteReceipt(WD.DELIVERY_UNCERTAIN, n, 1, WE.SHORT_WRITE),
            "invalid": lambda: WriteReceipt(WD.DELIVERY_UNCERTAIN, n, None, WE.INVALID_COUNT),
            "flush": lambda: WriteReceipt(WD.DELIVERY_UNCERTAIN, n, n, WE.FLUSH_ERROR),
            "missing": lambda: None,
            "length": lambda: WriteReceipt(WD.HOST_WRITE_COMPLETE, n + 1, n + 1),
        }[mode]()

    h = Harness(writer=writer)
    with pytest.raises(BaseException) as caught:
        h.begin()
    if mode in {"exception", "control"}:
        assert caught.value is original
    assert len(h.writes) == 1 and not h.ids and not bs(h)["wire_busy"]
    assert bs(h)["disconnect"] == ("not_written" if mode == "zero" else "uncertain")
    assert cs(h)["resync_required"] is (mode != "zero")
    h.writer = None
    assert h.owner.begin_bootstrap().accepted is (mode == "zero")


@pytest.mark.parametrize("phase", ["A", "B"])
def test_b12_zero_config_write_keeps_tombstone_and_late_drain(phase):
    def writer(data):
        kind, number = decode_write(data)
        if kind == "config":
            if len(h.ids) == (1 if phase == "A" else 2):
                return WriteReceipt(WD.DEFINITELY_NOT_WRITTEN, len(data), 0, WE.ZERO_WRITE)
            h.feed(complete(number))
        return ok(data)

    h = Harness(writer=writer)
    with pytest.raises(ms.ManagedWriteError):
        h.begin()
    assert bs(h)["wire_busy"] and not h.owner.request_config().accepted
    h.feed(complete(h.ids[-1]))
    assert not bs(h)["wire_busy"] and bs(h)["phase"] == "failed"
    assert len(h.ids) == (1 if phase == "A" else 2)


@pytest.mark.parametrize("phase", ["queued", "disconnect", "A", "B"])
def test_b13_clock_expiry_has_no_read_side_effects_or_late_revival(phase):
    clock = Clock()
    h = Harness(monotonic=clock)
    if phase == "queued":
        h.feed(b"\x94")
    elif phase == "disconnect":
        h.writer = lambda data: (setattr(clock, "value", 15), ok(data))[1]
    h.begin()
    if phase == "B":
        clock.value = 14
        h.feed(complete(h.ids[0]))
        assert bs(h)["remaining_seconds"] == 15
        clock.value = 29
    else:
        clock.value = 15
    count = len(h.writes), len(h.events)
    assert not h.b.config_complete and bs(h)["reason"] == "timeout"
    assert (len(h.writes), len(h.events)) == count
    h.owner.tick()
    if h.ids:
        h.feed(complete(h.ids[-1]))
    assert not h.b.config_complete and len(h.writes) == count[0]


@pytest.mark.parametrize("phase", ["queued", "A", "barrier", "B"])
def test_b14_cancel_is_finite_no_auto_resume_and_explicit_retry(phase):
    h = Harness()
    if phase == "queued":
        h.feed(b"\x94")
    request = h.begin()
    if phase in {"barrier", "B"}:
        h.feed(complete(h.ids[0]) + (b"\x94" if phase == "barrier" else b""))
    assert not h.owner.cancel_bootstrap(replace(request, session_id="stale"))
    assert h.owner.cancel_bootstrap(request)
    count = len(h.writes)
    if phase in {"queued", "barrier"}:
        h.feed(b"x")
    elif h.ids:
        h.feed(complete(h.ids[-1]))
    assert len(h.writes) == count and bs(h)["reason"] == "cancelled" and not h.b.config_complete
    later = h.owner.request_config() if phase == "B" else h.owner.begin_bootstrap()
    assert later.accepted


@pytest.mark.parametrize("limit", ["frames", "bytes", "debug"])
def test_b15_a_and_barrier_charge_discarded_input(limit):
    unknown = StreamFramer.frame(mp.field_varint(99, 1))
    opts = {"input_frames": 2} if limit == "frames" else {"input_bytes": len(unknown) * 2}
    h = Harness(limits=ms.ConfigLimits(**opts))
    h.begin()
    h.feed(unknown * 2)
    assert cs(h)["reason"] is None and h.b.nodes == {}
    h.feed(b"x" if limit == "debug" else unknown)
    assert cs(h)["reason"] == "capacity" and len(h.ids) == 1


def test_b15_b_inventory_budget_and_second_phase_counter_reset():
    h = Harness(limits=ms.ConfigLimits(nodes=1, input_frames=4))
    h.begin()
    h.feed(node(8) + node(9) + complete(h.ids[0]))
    assert cs(h)["input_frames"] == 0 and cs(h)["pending_nodes"] == 0
    h.feed(local() + node(100) + node(200))
    assert cs(h)["reason"] == "capacity" and not h.b.config_complete


def test_b16_retained_source_prefix_is_fully_charged_and_released():
    h = Harness()
    h.begin()
    h.feed(b"\x94\xc3\x00\x00" * (65536 // 4))
    assert len(h.b._rx_current) == cs(h)["queued_rx_bytes"] == 65536
    h.feed(b"x")
    assert cs(h)["resync_required"] and cs(h)["queued_rx_bytes"] == 0
    h.feed(complete(h.ids[0]))
    assert not h.owner.begin_bootstrap().accepted


@pytest.mark.parametrize("control", [False, True])
def test_b17_subscriber_exception_or_original_control(control):
    h = Harness()
    error = KeyboardInterrupt("subscriber") if control else RuntimeError("subscriber")

    def sink(kind, data):
        assert h.b._lock.acquire(blocking=False)
        h.b._lock.release()
        if kind == "mesh_config_status":
            raise error

    h.b._on_event = sink
    if control:
        with pytest.raises(KeyboardInterrupt) as caught:
            h.begin()
        assert caught.value is error and h.b.snapshot()["retired"]
        assert not cs(h)["cleanup_pending"] and cs(h)["queued_events"] == 0
    else:
        h.to_b()
        h.feed(local() + complete(h.ids[1]))
        assert h.b.config_complete


def test_b17_small_pump_budget_continues_only_on_explicit_tick():
    h = Harness(limits=ms.ConfigLimits(pump_actions=1))
    h.begin()
    assert len(h.writes) == 1
    for _ in range(4):
        h.owner.tick()
    h.feed(complete(h.ids[0]))
    for _ in range(8):
        h.owner.tick()
    assert len(h.ids) == 2


@pytest.mark.parametrize(
    "rank, expected",
    [
        (0, [1, 2]),
        (69418, [69419, 69422]),
        (69419, [69422, 69423]),
        (ms._ID_COUNT - 1, [0xFFFFFFFF, 1]),
    ],
)
def test_b18_seed_rank_and_wrap(rank, expected):
    writes = []
    token = object()
    owner = MeshWireOwner(
        OrderedSerialBinding(token, lambda _, data: (writes.append(data), ok(data))[1], True, True),
        randbelow=lambda n: rank,
    )
    owner.begin_bootstrap()
    a = decode_write(writes[-1])[1]
    owner.feed_bytes(token, complete(a))
    assert [decode_write(w)[1] for w in writes[1:]] == expected


@pytest.mark.parametrize("rank", [True, -1, ms._ID_COUNT, 1.0, "1"])
def test_b18_invalid_nonce_factory_does_not_write(rank):
    binding = OrderedSerialBinding(object(), lambda *_: pytest.fail("writer entered"), True, True)
    owner = MeshWireOwner(binding, randbelow=lambda _: rank)
    assert owner.begin_bootstrap().reason == "nonce_unavailable"


@pytest.mark.parametrize("remaining", [0, 1, 2])
def test_b18_two_id_exhaustion_preflight(remaining):
    h = Harness()
    h.owner.retire()
    state = h.owner.export_state()
    issued = ms._ID_COUNT - remaining
    a = ms._advance_id(state.wire.seed, issued - 2)
    b = ms._advance_id(state.wire.seed, issued - 1)
    # A coherent historical completed pair, then a gap, represents the exhaustion boundary.
    # The original fixture inflated issued while retaining an untouched zero-operation record.
    state = replace(
        state, wire=replace(state.wire, issued=issued), operation=1, sync_id=a,
        inventory_id=b, phase="ready", disconnect="host_complete",
        last_config=ms.ConfigOutcome(h.b.session_id, 2, b, "ready", None))
    writes, token = [], object()
    owner = MeshWireOwner(
        OrderedSerialBinding(token, lambda _, d: (writes.append(d), ok(d))[1], True, True),
        state=state,
        randbelow=lambda _: pytest.fail("reseed"),
    )
    request = owner.begin_bootstrap()
    assert request.accepted is (remaining == 2)
    if request.accepted:
        owner.feed_bytes(token, complete(request.request_id))
        assert len(writes) == 3
    else:
        assert writes == [] and request.reason == "id_exhausted"


@pytest.mark.parametrize("phase", ["A", "B", "ready"])
def test_b19_transfer_retains_admission_allocator_and_drain_without_restart(phase):
    h = Harness()
    if phase == "ready":
        h.ready()
    elif phase == "B":
        h.to_b()
    else:
        h.begin()
    with pytest.raises(RuntimeError):
        h.owner.export_state()
    h.owner.retire()
    state = h.owner.export_state()
    token, writes = object(), []
    other = MeshWireOwner(
        OrderedSerialBinding(token, lambda _, data: (writes.append(data), ok(data))[1], True, True),
        state=state,
        randbelow=lambda _: pytest.fail("reseed"),
    )
    assert other.snapshot()["nodes"] == [] and writes == []
    if state.wire.outstanding_id:
        assert not other.begin_bootstrap().accepted and not other.request_config().accepted
        other.feed_bytes(token, complete(state.wire.outstanding_id))
        assert writes == []
    request = other.begin_bootstrap() if phase == "A" else other.request_config()
    assert request.accepted and request.request_id not in h.ids


def test_b19_missing_old_envelope_and_scheme_refused():
    h = Harness()
    h.owner.retire()
    state = h.owner.export_state()
    for broken in [state.wire, object()]:
        with pytest.raises(ValueError):
            MeshWireOwner(h.owner._binding, state=broken)
    with pytest.raises(ValueError):
        replace(state, scheme="old")
    with pytest.raises(ValueError):
        replace(state.wire, scheme="old")
    for name in ("scheme", "profile"):
        missing = copy.copy(state)
        object.__delattr__(missing, name)
        with pytest.raises(ValueError):
            MeshWireOwner(h.owner._binding, state=missing)
    with pytest.raises(ValueError):
        ms.MeshtasticBackend(
            lambda _: None, managed=True, bootstrap_token=object(), wire_state=state.wire
        )


def test_b20_b22_known_loss_is_not_repaired_by_marker_or_new_owner():
    h = Harness()
    h.begin()
    h.owner.transport_lost()
    state = h.owner.export_state()
    other = MeshWireOwner(h.owner._binding, state=state)
    other.feed_bytes(h.incarnation, complete(h.ids[0]))
    assert not other.begin_bootstrap().accepted and not other.request_config().accepted
    assert other.snapshot()["config_status"]["resync_required"]


def test_b21_profile_before_metadata_echo_and_stale_rx():
    assert MeshWireOwner(object()).begin_bootstrap().reason == "profile_unavailable"
    for kwargs in [dict(exclusive=False, ordered=True), dict(exclusive=True, ordered=False)]:
        with pytest.raises(ValueError):
            OrderedSerialBinding(object(), lambda *_: None, **kwargs)
    h = Harness()
    h.begin()
    h.feed(h.writes[-1])  # ToRadio echo is not a valid MyNodeInfo.
    assert not bs(h)["identified"]
    h.owner.feed_bytes(object(), local() + complete(h.ids[0]))
    assert not bs(h)["identified"] and len(h.ids) == 1
    h.feed(local())
    assert bs(h)["identified"] and not h.b.config_complete and h.b.nodes == {}


@pytest.mark.parametrize(
    "phase", ["initial", "A", "barrier", "B", "ready", "refresh", "failed", "retired"]
)
def test_b31_managed_public_and_generic_sends_refuse_without_writer_or_false_success(phase):
    h = Harness()
    if phase in {"ready", "refresh"}:
        h.ready()
        if phase == "refresh":
            h.owner.request_config()
    elif phase != "initial":
        request = h.begin()
        if phase in {"barrier", "B"}:
            h.feed(complete(h.ids[0]) + (b"\x94" if phase == "barrier" else b""))
        elif phase == "failed":
            h.owner.cancel_bootstrap(request)
        elif phase == "retired":
            h.owner.retire()
    before = len(h.writes), h.b.config_id, h.b.snapshot()
    for call in [
        lambda: h.b.send_text("private"),
        h.b.send_heartbeat,
        lambda: h.b._write_payload(mp.encode_disconnect()),
    ]:
        with pytest.raises(ms.ManagedWriteUnavailable):
            call()
    assert (len(h.writes), h.b.config_id, h.b.snapshot()) == before
    h.b.close()
    h.b.close()
    assert len(h.writes) == before[0] and h.b.snapshot()["retired"]


def test_b31_reentrant_send_and_close_during_disconnect_do_not_write_a():
    def writer(data):
        with pytest.raises(ms.ManagedWriteUnavailable):
            h.b.send_heartbeat()
        h.b.close()
        return ok(data)

    h = Harness(writer=writer)
    h.begin()
    assert len(h.writes) == 1 and h.b.snapshot()["retired"] and not cs(h)["cleanup_pending"]


def test_b31_standalone_send_heartbeat_and_close_remain_supported():
    writes = []
    backend = ms.MeshtasticBackend(writes.append)
    backend.send_text("standalone")
    backend.send_heartbeat()
    backend.close()
    assert len(writes) == 3


def test_b11_clock_control_after_d_keeps_actual_completed_receipt():
    clock = Clock()
    control = KeyboardInterrupt("clock after D")
    h = Harness(monotonic=clock)

    def writer(data):
        h.b._clock = lambda: (_ for _ in ()).throw(control)
        return ok(data)

    h.writer = writer
    with pytest.raises(KeyboardInterrupt) as caught:
        h.begin()
    assert caught.value is control
    h.b._clock = clock
    assert bs(h)["disconnect"] == "host_complete" and not bs(h)["wire_busy"]
    assert h.b.snapshot()["retired"] and len(h.writes) == 1


def test_b11_d_control_preserves_identity_when_cleanup_also_controls(monkeypatch):
    first, second = KeyboardInterrupt("original"), SystemExit("cleanup")
    h = Harness(writer=lambda _: (_ for _ in ()).throw(first))
    monkeypatch.setattr(h.b._framer, "reset", lambda: (_ for _ in ()).throw(second))
    with pytest.raises(KeyboardInterrupt) as caught:
        h.begin()
    assert caught.value is first and cs(h)["cleanup_pending"]
    assert bs(h)["disconnect"] == "uncertain"
    monkeypatch.undo()
    h.owner.tick()
    assert not cs(h)["cleanup_pending"]


@pytest.mark.parametrize("kind", ["malformed", "invalid_length", "loss"])
def test_b15_malformed_and_lost_input_cannot_publish_partial_inventory(kind):
    h = Harness()
    h.to_b()
    h.feed(local() + node())
    if kind == "loss":
        h.owner.transport_lost()
    else:
        h.feed(
            StreamFramer.frame(mp.field_varint(3, 1))
            if kind == "malformed"
            else b"\x94\xc3\xff\xff"
        )
    h.feed(complete(h.ids[-1]))
    assert not h.b.config_complete and h.b.nodes == {}
    assert cs(h)["reason"] in {"malformed", "retired"}


@pytest.mark.parametrize("extra", [False, True])
def test_b15_full_node_and_channel_limits_through_b(extra):
    h = Harness()
    h.to_b()
    h.feed(
        local()
        + b"".join(node(i) for i in range(100, 612))
        + b"".join(channel(i) for i in range(8))
    )
    for _ in range(3):
        h.owner.tick()  # More than 256 decoded rows intentionally needs another bounded pump turn.
    assert cs(h)["pending_nodes"] == 512 and cs(h)["pending_channels"] == 8
    if extra:
        h.feed(channel(8))
    h.feed(complete(h.ids[1]))
    assert h.b.config_complete is (not extra)
    assert len(h.b.nodes) == (0 if extra else 512)


@pytest.mark.parametrize("cap", ["bytes", "chunks"])
def test_b16_receive_capacity_while_d_writer_owns_pump(cap):
    options = {"rx_bytes": 8} if cap == "bytes" else {"rx_chunks": 2}

    def writer(data):
        if cap == "bytes":
            h.feed(b"x" * 8)
            h.feed(b"x")
        else:
            h.feed(b"x")
            h.feed(b"x")
            h.feed(b"x")
        return ok(data)

    h = Harness(writer=writer, limits=ms.ConfigLimits(**options))
    h.begin()
    assert cs(h)["resync_required"] and cs(h)["reason"] == "capacity"
    assert len(h.writes) == 1 and not h.b.config_complete


def test_b16_full_rx_charge_release_positive_control():
    h = Harness(limits=ms.ConfigLimits(parser_slice=4, pump_actions=2, rx_bytes=8))
    for _ in range(3):
        h.feed(b"\x94\xc3\x00\x00" * 2)
        assert len(h.b._rx_current) == cs(h)["queued_rx_bytes"] == 8
        h.owner.tick()
        assert cs(h)["queued_rx_bytes"] == 0 and not cs(h)["resync_required"]


def test_b18_invalid_nonce_error_and_control_keep_exact_policy():
    binding = OrderedSerialBinding(object(), lambda *_: pytest.fail("write"), True, True)
    owner = MeshWireOwner(binding, randbelow=lambda _: (_ for _ in ()).throw(ValueError("seed")))
    assert owner.begin_bootstrap().reason == "nonce_unavailable"
    control = KeyboardInterrupt("randomness")
    with pytest.raises(KeyboardInterrupt) as caught:
        MeshWireOwner(binding, randbelow=lambda _: (_ for _ in ()).throw(control))
    assert caught.value is control


def test_b18_cancelled_queued_sync_id_not_refunded():
    h = Harness()
    h.feed(b"\x94")
    request = h.begin()
    assert not h.writes and h.owner.cancel_bootstrap(request)
    h.feed(b"x")
    second = h.begin()
    assert second.request_id == ms._advance_id(request.request_id, 1)


def test_b22_confirmed_source_change_retires_managed_refresh():
    h = Harness()
    h.ready()
    h.owner.request_config()
    h.feed(local(200) + node(200) + complete(h.ids[-1]))
    assert h.b.snapshot()["retired"] and not h.b.config_complete
    assert list(h.b.nodes) == [100]


@pytest.mark.parametrize("target", ["D", "A", "B"])
def test_b31_reentry_during_each_writer_cannot_bypass_serialization(target):
    def writer(data):
        kind, number = decode_write(data)
        phase = "D" if kind == "disconnect" else "A" if len(h.ids) == 1 else "B"
        if phase == target:
            before = len(h.writes)
            for call in [
                lambda: h.b.send_text(object()),
                h.b.send_heartbeat,
                lambda: h.b._write_payload(b"untrusted"),
            ]:
                with pytest.raises(ms.ManagedWriteUnavailable):
                    call()
            assert len(h.writes) == before
        if kind == "config":
            h.feed(local() + complete(number))
        return ok(data)

    h = Harness(writer=writer)
    h.begin()
    assert len(h.writes) == 3 and h.b.config_complete


def test_b22_observation_gap_reduces_trust_preserves_history_and_requires_explicit_begin():
    h = Harness()
    h.ready()
    before, old_wire = bs(h), h.b._wire
    writes, events = len(h.writes), len(h.events)
    token, incarnation, session = h.owner._token, h.owner._binding.incarnation, h.b.session_id
    assert not h.b.mark_observation_gap(object())
    assert h.owner.mark_observation_gap()
    assert len(h.writes) == writes and len(h.events) == events
    assert h.b._wire == old_wire and h.owner._token is token
    assert h.owner._binding.incarnation is incarnation and h.b.session_id == session
    assert not h.b.config_complete and cs(h)["inventory_stale"]
    assert not bs(h)["publication_admitted"] and bs(h)["admission_state"] == "unverified_idle"
    assert bs(h)["phase"] == before["phase"] == "ready"  # historical operation unchanged
    assert bs(h)["sync_id"] == before["sync_id"] and bs(h)["inventory_id"] == before["inventory_id"]
    assert list(h.b.nodes) == [100]  # the separately marked stale view, never an A candidate
    assert not h.owner.request_config().accepted
    request = h.begin()
    assert request.request_id == ms._advance_id(h.ids[1], 1)
    h.feed(local(8) + node(8) + complete(request.request_id))
    assert list(h.b.nodes) == [100] and not h.b.config_complete
    h.feed(local() + node(200) + complete(h.ids[-1]))
    assert h.b.config_complete and list(h.b.nodes) == [200]


def test_b22_gap_envelope_transfer_does_not_reseed_or_rebind_previous_owner():
    h = Harness()
    h.ready()
    assert h.owner.mark_observation_gap()
    h.owner.retire()
    state = h.owner.export_state()
    assert state.phase == "ready" and not state.synchronized
    current, writes = object(), []
    owner = MeshWireOwner(
        OrderedSerialBinding(
            current, lambda _, data: (writes.append(data), ok(data))[1], True, True
        ),
        state=state,
        randbelow=lambda _: pytest.fail("reseed"),
    )
    assert owner.snapshot()["bootstrap_status"]["admission_state"] == "unverified_idle"
    assert owner.snapshot()["nodes"] == [] and not owner.request_config().accepted
    request = owner.begin_bootstrap()
    assert request.accepted and request.request_id == ms._advance_id(h.ids[-1], 1)
    owner.feed_bytes(h.incarnation, complete(request.request_id))
    assert len(writes) == 2
    owner.feed_bytes(current, complete(request.request_id))
    assert len(writes) == 3


@pytest.mark.parametrize(
    "phase", ["A", "B", "refresh", "drain", "partial", "debug", "loss", "retired"]
)
def test_b22_gap_refuses_active_partial_and_uncertain_ownership(phase):
    h = Harness()
    if phase == "A":
        h.begin()
    elif phase == "B":
        h.to_b()
    else:
        h.ready()
        if phase in {"refresh", "drain"}:
            request = h.owner.request_config()
            if phase == "drain":
                h.b.cancel_config(request.attempt_id)
        elif phase == "partial":
            h.feed(b"\x94")
        elif phase == "debug":
            h.feed(b"unterminated debug")
        elif phase == "loss":
            h.owner.transport_lost()
        elif phase == "retired":
            h.owner.retire()
    before = h.b._wire, bs(h), len(h.writes)
    assert not h.owner.mark_observation_gap()
    assert (h.b._wire, bs(h), len(h.writes)) == before


def test_b22_gap_refuses_while_writer_lease_runs_and_preserves_failed_operation():
    h = Harness()
    h.ready()
    captured = []

    def writer(data):
        captured.append(h.owner.mark_observation_gap())
        return ok(data)

    h.writer = writer
    request = h.owner.request_config()
    assert captured == [False]
    h.b.cancel_config(request.attempt_id)
    h.feed(complete(request.request_id))
    before = bs(h)
    failed = cs(h)
    assert h.owner.mark_observation_gap()
    assert bs(h)["phase"] == before["phase"] and bs(h)["reason"] == before["reason"]
    assert cs(h)["reason"] == failed["reason"] == "cancelled"
    assert cs(h)["request_id"] == failed["request_id"]


def test_b22_gap_refuses_uncertain_disconnect_without_erasing_record():
    h = Harness(writer=lambda _: None)
    with pytest.raises(ms.ManagedWriteError):
        h.begin()
    before = h.b._wire, bs(h), len(h.writes)
    assert not h.owner.mark_observation_gap()
    assert (h.b._wire, bs(h), len(h.writes)) == before
