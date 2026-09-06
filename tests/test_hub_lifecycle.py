"""A retired communication hub must leave shared devices and observers usable."""

from __future__ import annotations

import threading
import types

import pytest

from src.core.cross_comm import EventBus, RoutingRule, TargetPool
from src.core.cross_comm_hub import CrossCommHub
from src.core.device_manager import DeviceManager
from src.core.lifecycle import ScopeClosedError
from src.models.device import Device


class InertConnection:
    port = "COM_LIFECYCLE_TEST"
    is_connected = True
    raw = False

    def __init__(self):
        self.lines, self.bytes, self.states = [], [], []

    def on_line(self, cb):
        self.lines.append(cb)

    def remove_line_callback(self, cb):
        if cb in self.lines:
            self.lines.remove(cb)

    def on_bytes(self, cb):
        self.bytes.append(cb)

    def remove_byte_callback(self, cb):
        if cb in self.bytes:
            self.bytes.remove(cb)

    def on_state_change(self, cb):
        self.states.append(cb)

    def remove_state_callback(self, cb):
        if cb in self.states:
            self.states.remove(cb)

    def write(self, _command):
        raise AssertionError("test connections must not write hardware")

    write_bytes = write


def environment(firmware="marauder"):
    dm, bus = DeviceManager(), EventBus()
    pool, conn = TargetPool(bus), InertConnection()
    dm.add_device(Device(port=conn.port, firmware=firmware))
    dm._connections[conn.port] = conn
    return dm, bus, pool, conn


def test_replacement_hub_ingests_once_and_preserves_foreign_callbacks():
    dm, bus, pool, conn = environment()
    observed = []
    foreign = lambda *args: observed.append(args)
    dm.on_connection_opened(foreign)
    bus.subscribe("*", foreign)
    conn.on_line(foreign)
    old = CrossCommHub(dm, bus, pool)
    dm._fire_conn_opened(conn.port, conn)
    copied = conn.lines[-1]
    old.close()
    old.close()
    assert conn.is_connected
    assert dm.get_connection(conn.port) is conn
    assert conn.lines == [foreign]
    assert dm._on_conn_opened == [foreign]
    assert bus.topics == ["*"]
    copied("AP: Retired BSSID: DE:AD:BE:EF:00:11 Ch: 6 RSSI: -42")
    assert pool.count == 0
    new = CrossCommHub(dm, bus, pool)
    try:
        dm._fire_conn_opened(conn.port, conn)
        observed.clear()
        for callback in tuple(conn.lines):
            callback("AP: Current BSSID: DE:AD:BE:EF:00:11 Ch: 6 RSSI: -42")
        topics = [args[0] for args in observed if len(args) == 2]
        assert topics == ["target.added"]
        assert pool.count == 1
    finally:
        new.close()


def test_copied_device_callback_cannot_reattach_closed_hub():
    dm, bus, pool, conn = environment()
    hub = CrossCommHub(dm, bus, pool)
    copied = dm._on_conn_opened[0]
    hub.close()
    copied(conn.port, conn)
    assert conn.lines == []
    with pytest.raises(ScopeClosedError):
        hub.ingestor.attach(conn, object())


def test_inflight_parser_finishes_before_close_and_old_callback_is_inert():
    dm, bus, pool, conn = environment()
    hub = CrossCommHub(dm, bus, pool)
    started, release = threading.Event(), threading.Event()
    sent = []
    hub.router._send = lambda *args: sent.append(args)
    hub.router.add_rule(RoutingRule(name="inert", device_port=conn.port,
                                   command_template="INERT-NO-IO", cooldown=0))

    def parse(_line):
        started.set()
        assert release.wait(5)
        return None

    callback = hub.ingestor.attach(conn, types.SimpleNamespace(parse_line=parse))
    worker = threading.Thread(target=lambda: callback("synthetic"))
    worker.start()
    try:
        assert started.wait(5)
        with pytest.raises(TimeoutError):
            hub.close(timeout=0)
        bus.publish("target.added", {"mac": "02:00:00:00:00:01", "rssi": -30})
        assert sent == []
        assert hub.router._subscriptions.closed and hub.correlator._subscriptions.closed
        assert conn.lines == [callback]
    finally:
        release.set()
        worker.join(5)
    hub.close()
    assert conn.lines == []
    callback("now inert")


@pytest.mark.parametrize("stage", ["SensingModel", "BroadcastEngine"])
def test_hub_construction_failure_removes_partial_subscriptions(monkeypatch, stage):
    dm, bus, pool, _ = environment()

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic constructor failure")

    if stage == "SensingModel":
        monkeypatch.setattr("src.core.cross_comm_hub.SensingModel", fail)
    else:
        monkeypatch.setattr("src.core.broadcast.BroadcastEngine", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        CrossCommHub(dm, bus, pool)
    assert dm._on_conn_opened == []
    assert dm._on_changed == []
    assert bus.topics == []


def test_stream_close_removes_owned_callbacks_without_resetting_replacement(monkeypatch):
    dm, bus, pool, conn = environment("meshtastic")
    backend = types.SimpleNamespace(feed_bytes=lambda data: None, start=lambda: None)
    monkeypatch.setattr("src.protocols.meshtastic_stream.MeshtasticBackend", lambda *a, **k: backend)
    hub = CrossCommHub(dm, bus, pool)
    dm._fire_conn_opened(conn.port, conn)
    assert len(conn.bytes) == len(conn.states) == 1
    replacement = object()
    conn.mesh_backend = replacement
    hub.close()
    assert conn.bytes == conn.states == conn.lines == []
    assert conn.mesh_backend is replacement
    assert conn.raw is True


def test_stream_start_failure_rolls_back_partial_callbacks(monkeypatch):
    dm, bus, pool, conn = environment("meshtastic")

    def fail():
        raise RuntimeError("synthetic stream startup failure")

    backend = types.SimpleNamespace(feed_bytes=lambda data: None, start=fail)
    monkeypatch.setattr("src.protocols.meshtastic_stream.MeshtasticBackend", lambda *a, **k: backend)
    hub = CrossCommHub(dm, bus, pool)
    try:
        dm._fire_conn_opened(conn.port, conn)
        assert conn.bytes == conn.states == []
        assert conn.raw is False
        assert hub.mesh_backends == hub._mesh_conns == {}
    finally:
        hub.close()


def test_ingestor_close_reports_failed_detach_and_can_retry(monkeypatch):
    dm, bus, pool, conn = environment()
    hub = CrossCommHub(dm, bus, pool)
    dm._fire_conn_opened(conn.port, conn)
    callback = conn.lines[0]
    remove = conn.remove_line_callback

    def fail(_callback):
        raise RuntimeError("synthetic detach failure")

    monkeypatch.setattr(conn, "remove_line_callback", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        hub.close()
    callback("AP: Closed BSSID: DE:AD:BE:EF:00:11 Ch: 6 RSSI: -42")
    assert pool.count == 0
    monkeypatch.setattr(conn, "remove_line_callback", remove)
    hub.close()
    assert conn.lines == []


def test_correlator_partial_construction_unwinds_earlier_registration(monkeypatch):
    dm, bus, pool, _conn = environment()
    subscribe = bus.subscribe

    def fail_second(topic, callback):
        if topic == "capture.added":
            raise ValueError("synthetic second subscription failure")
        subscribe(topic, callback)

    monkeypatch.setattr(bus, "subscribe", fail_second)
    with pytest.raises(ValueError, match="second subscription"):
        CrossCommHub(dm, bus, pool)
    assert bus.topics == []


def test_failed_line_registration_is_still_owned_and_removable():
    dm, bus, pool, conn = environment()
    hub = CrossCommHub(dm, bus, pool)

    def install(callback):
        conn.lines.append(callback)
        raise RuntimeError("synthetic append then raise")

    conn.on_line = install
    with pytest.raises(RuntimeError, match="append then raise"):
        hub.ingestor.attach(conn, types.SimpleNamespace(parse_line=lambda line: None))
    hub.close()
    assert conn.lines == []


def test_late_old_mesh_detach_preserves_new_connection_registration(monkeypatch):
    from src.core.serial_handler import ConnectionState

    dm, bus, pool, old = environment("meshtastic")
    monkeypatch.setattr("src.protocols.meshtastic_stream.MeshtasticBackend", lambda *a, **k:
                        types.SimpleNamespace(feed_bytes=lambda data: None, start=lambda: None))
    hub = CrossCommHub(dm, bus, pool)
    dm._fire_conn_opened(old.port, old)
    old_state = old.states[0]
    started, release = threading.Event(), threading.Event()
    calls, errors = [], []
    lock = threading.Lock()
    remove = old.remove_byte_callback

    def delayed_remove(callback):
        with lock:
            calls.append(True)
            first = len(calls) == 1
        if first:
            started.set()
            assert release.wait(5)
        remove(callback)

    old.remove_byte_callback = delayed_remove

    def disconnect():
        try:
            old_state(ConnectionState.DISCONNECTED)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=disconnect)
    worker.start()
    new = InertConnection()
    try:
        assert started.wait(5)
        dm._connections[new.port] = new
        dm._fire_conn_opened(new.port, new)
        replacement = new.mesh_backend
    finally:
        release.set()
        worker.join(5)
    assert not errors and not worker.is_alive()
    assert hub.mesh_backend(new.port) is replacement
    hub.close()
    assert new.lines == new.bytes == new.states == []


def test_old_mesh_state_callback_cannot_detach_new_backend_on_same_connection(monkeypatch):
    from src.core.serial_handler import ConnectionState

    dm, bus, pool, conn = environment("meshtastic")
    monkeypatch.setattr("src.protocols.meshtastic_stream.MeshtasticBackend", lambda *a, **k:
                        types.SimpleNamespace(feed_bytes=lambda data: None, start=lambda: None))
    hub = CrossCommHub(dm, bus, pool)
    try:
        dm._fire_conn_opened(conn.port, conn)
        old_backend, old_state = conn.mesh_backend, conn.states[0]
        device = dm.get_device(conn.port)
        device.firmware = "marauder"
        hub._on_device_changed(device)
        device.firmware = "meshtastic"
        hub._on_device_changed(device)
        new_backend = conn.mesh_backend
        new_bytes, new_states = tuple(conn.bytes), tuple(conn.states)
        assert new_backend is not old_backend
        old_state(ConnectionState.DISCONNECTED)
        assert hub.mesh_backend(conn.port) is conn.mesh_backend is new_backend
        assert conn.raw
        assert tuple(conn.bytes) == new_bytes and tuple(conn.states) == new_states
    finally:
        hub.close()
    assert conn.bytes == conn.states == []


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("component", ["correlator", "hub"])
def test_constructor_preserves_control_exception_if_cleanup_also_fails(monkeypatch, failure, component):
    from src.core.capture_correlate import CaptureCorrelator

    dm, bus, pool, _conn = environment()
    original, cleanup_error = failure("synthetic construction interruption"), RuntimeError("cleanup failed")
    subscribe = bus.subscribe

    def fail_second(topic, callback):
        if topic == "capture.added":
            raise original
        subscribe(topic, callback)

    def fail_remove(topic, callback):
        if topic == "action.executed":
            raise cleanup_error

    if component == "correlator":
        monkeypatch.setattr(bus, "subscribe", fail_second)
        monkeypatch.setattr(bus, "unsubscribe", fail_remove)
        construct = lambda: CaptureCorrelator(bus)
    else:
        monkeypatch.setattr(CrossCommHub, "_initialize", lambda *_: (_ for _ in ()).throw(original))
        monkeypatch.setattr(CrossCommHub, "close", lambda *_: (_ for _ in ()).throw(cleanup_error))
        construct = lambda: CrossCommHub(dm, bus, pool)
    with pytest.raises(failure) as caught:
        construct()
    assert caught.value is original and caught.value.__cause__ is cleanup_error
