"""Detected firmware and parser ownership must agree without touching hardware."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.core.cross_comm import EventBus, TargetPool
from src.core.cross_comm_hub import CrossCommHub
from src.core.device_manager import DeviceManager
from src.core.handshake import HandshakeResult
from src.core.target_ingest import TargetIngestor
from src.models.device import Device, Protocol
from src.protocols import get_protocol
from src.protocols.base import ParsedEvent


class InertConnection:
    def __init__(self, port="INERT_BLE"):
        self.port = port
        self.is_connected = True
        self.lines = []
        self.states = []

    def on_line(self, callback):
        self.lines.append(callback)

    def remove_line_callback(self, callback):
        if callback in self.lines:
            self.lines.remove(callback)

    def on_state_change(self, callback):
        self.states.append(callback)

    def remove_state_callback(self, callback):
        if callback in self.states:
            self.states.remove(callback)

    def feed(self, line):
        for callback in list(self.lines):
            callback(line)

    def write(self, *args, **kwargs):
        raise AssertionError("No device commands in this fixture")

    def disconnect(self):
        self.is_connected = False


BLE_LINES = {
    "ghost-esp": "BLE Device: 02:00:00:00:00:01 Name: Watch RSSI: -48",
    "halehound": "[BLE] Name: Watch | ADDR: 02:00:00:00:00:01 | RSSI: -60 | Type: Random",
    "lxveos": "LXVEOS/1 ble addr=02:00:00:00:00:01 type=random rssi=-55 name=5761746368",
}


@pytest.fixture
def wired():
    dm = DeviceManager()
    hub = CrossCommHub(dm)
    conn = InertConnection()
    device = Device(port=conn.port)
    dm.attach_connection(device, conn)
    yield dm, hub, conn, device
    hub.close()


@pytest.mark.parametrize("firmware", BLE_LINES)
def test_detection_after_open_reconciles_real_ble_parser(wired, firmware):
    dm, hub, conn, _ = wired
    assert hub.ingestor.parser_for(conn.port).protocol_name == "marauder"
    dm.set_firmware(conn.port, firmware)
    conn.feed(BLE_LINES[firmware])
    assert hub.pool.count == 1
    assert hub.pool.all()[0].ssid == "Watch"


def test_real_probe_publishes_detection_and_reconciles_parser(wired, monkeypatch):
    from src.core import handshake
    dm, hub, conn, device = wired
    monkeypatch.setattr(handshake, "_wait_for_reply", lambda *args, **kwargs: None)

    def reply(_command):
        for line in ("GhostESP v1.0.0", "  scanwifi", "  stopscan", "  help"):
            conn.feed(line)

    monkeypatch.setattr(conn, "write", reply)
    changes = []
    dm.on_device_changed(lambda dev: changes.append(dev.firmware))
    result = dm.probe(conn.port, timeout=0)
    assert device.firmware == "ghost-esp" and device.protocol is Protocol.GHOST_ESP
    assert device.health == result.health == "alive"
    assert device.fw_banner == result.banner == "GhostESP v1.0.0"
    assert changes == ["ghost-esp"]
    conn.feed(BLE_LINES["ghost-esp"])
    assert hub.pool.count == 1


def test_same_protocol_notifications_preserve_parser_ordinals_and_history(wired):
    dm, hub, conn, _ = wired
    parser = hub.ingestor.parser_for(conn.port)
    conn.feed("AP: First BSSID: 02:00:00:00:00:01 Ch: 1 RSSI: -60")
    conn.feed("-60 Device: Watch")
    prior = hub.ingestor.ble_observations()[0]
    dm.set_firmware(conn.port, "MARAUDER", forced=True)
    dm.set_detected_chip(conn.port, "esp32")
    assert hub.ingestor.parser_for(conn.port) is parser
    conn.feed("AP: Second BSSID: 02:00:00:00:00:02 Ch: 1 RSSI: -61")
    assert hub.pool.all()[-1].extra["index"] == 1
    conn.feed("-61 Device: Watch")
    later = hub.ingestor.ble_observations()[-1]
    assert later["connection_epoch"] == prior["connection_epoch"]
    assert later["scan_epoch"] == prior["scan_epoch"]
    dm.set_firmware(conn.port, "ghost-esp")
    parser = hub.ingestor.parser_for(conn.port)
    dm.set_firmware(conn.port, "Ghost_ESP")
    assert hub.ingestor.parser_for(conn.port) is parser


def test_text_switch_retains_reports_and_rejects_old_copied_callback(wired):
    dm, hub, conn, _ = wired
    old = conn.lines[0]
    conn.feed("-60 Device: Before switch")
    dm.set_firmware(conn.port, "ghost-esp")
    old("BLE: 02:00:00:00:00:02 Name: Stale RSSI: -20")
    conn.feed(BLE_LINES["ghost-esp"])
    assert hub.pool.count == 1 and hub.pool.all()[0].ssid == "Watch"
    assert hub.ingestor.ble_observations()[0]["label"] == "Before switch"
    assert len(conn.lines) == 1


def test_replacement_connection_and_device_ignore_old_callbacks(wired):
    dm, hub, conn, _ = wired
    dm.set_firmware(conn.port, "lxveos")
    old = conn.lines[0]
    replacement = InertConnection(conn.port)
    device = Device(port=conn.port, firmware="ghost-esp")
    dm.attach_connection(device, replacement)
    old("LXVEOS/1 status board=stale chip=esp32 fw=old caps=0x3 arm=armed")
    old(BLE_LINES["lxveos"])
    replacement.feed(BLE_LINES["ghost-esp"])
    assert device.telemetry == {} and device.arm_state == ""
    assert hub.pool.count == 1 and len(conn.lines) == 0
    assert len(replacement.lines) == 1


def test_same_connection_rebound_to_new_device_replaces_stale_identity(wired):
    dm, hub, conn, _ = wired
    old = conn.lines[0]
    dm.attach_connection(Device(port=conn.port), conn)
    assert conn.lines[0] is not old
    old("-60 Device: Stale")
    conn.feed("-60 Device: Current")
    assert [r["label"] for r in hub.ingestor.ble_observations()] == ["Current"]


def test_old_inflight_parse_is_discarded_after_attachment_replacement():
    entered, resume = threading.Event(), threading.Event()

    class BlockingProtocol:
        def parse_line(self, line):
            entered.set()
            assert resume.wait(3)
            return ParsedEvent("ble_found", {"mac": "02:00:00:00:00:01", "name": "Old"})

    pool = TargetPool(EventBus())
    ingestor = TargetIngestor(pool)
    conn = InertConnection()
    old = ingestor.attach(conn, BlockingProtocol())
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(old, "old in-flight line")
        assert entered.wait(3)
        try:
            ingestor.attach(conn, get_protocol("ghost-esp"))
        finally:
            resume.set()
        future.result(timeout=3)
    assert pool.count == 0
    conn.feed(BLE_LINES["ghost-esp"])
    assert pool.count == 1
    ingestor.close()


@pytest.mark.parametrize("failure", [RuntimeError("install"), KeyboardInterrupt("install")])
def test_append_then_raise_replacement_keeps_prior_parser_and_retries_cleanup(failure):
    conn = InertConnection()
    ingestor = TargetIngestor(TargetPool(EventBus()))
    old_parser = get_protocol("marauder")
    old = ingestor.attach(conn, old_parser)
    normal_install, normal_remove = conn.on_line, conn.remove_line_callback
    removed = []

    def fail_install(callback):
        normal_install(callback)
        raise failure

    def fail_remove(callback):
        removed.append(callback)
        raise RuntimeError("temporary remove failure")

    conn.on_line, conn.remove_line_callback = fail_install, fail_remove
    with pytest.raises(type(failure)) as caught:
        ingestor.attach(conn, get_protocol("ghost-esp"))
    assert caught.value is failure
    assert ingestor.parser_for(conn.port) is old_parser
    assert conn.lines[0] is old and len(conn.lines) == 2
    conn.lines[1](BLE_LINES["ghost-esp"])
    conn.feed("-60 Device: Retained")
    assert [r["label"] for r in ingestor.ble_observations()] == ["Retained"]
    conn.remove_line_callback = normal_remove
    ingestor.close()
    assert conn.lines == [] and len(removed) == 1


@pytest.mark.parametrize("supersede", ["replace", "rebind-same", "manual-choice", "newer-probe"])
def test_stale_probe_cannot_publish_over_current_binding(wired, monkeypatch, supersede):
    from src.core import handshake
    dm, hub, conn, device = wired
    entered, resume = threading.Event(), threading.Event()
    calls = []

    def probe(_conn, candidate, **_kwargs):
        ordinal = len(calls)
        calls.append(candidate)
        if ordinal == 0:
            entered.set()
            assert resume.wait(3)
        candidate.firmware = "ghost-esp" if ordinal == 0 else "halehound"
        candidate.protocol = Protocol.GHOST_ESP if ordinal == 0 else Protocol.HALEHOUND
        candidate.health = "alive"
        candidate.fw_banner = "old" if ordinal == 0 else "new"
        return HandshakeResult(health="alive", banner=candidate.fw_banner)

    monkeypatch.setattr(handshake, "probe_device", probe)
    with ThreadPoolExecutor(max_workers=1) as executor:
        old = executor.submit(dm.probe, conn.port)
        assert entered.wait(3)
        try:
            if supersede == "replace":
                device = Device(port=conn.port, firmware="halehound")
                dm.attach_connection(device, InertConnection(conn.port))
            elif supersede == "rebind-same":
                dm.attach_connection(device, conn)
            elif supersede == "manual-choice":
                dm.set_firmware(conn.port, "halehound", forced=True)
            else:
                assert dm.probe(conn.port).banner == "new"
        finally:
            resume.set()
        assert old.result(timeout=3) is None
    expected = "marauder" if supersede == "rebind-same" else "halehound"
    assert hub.ingestor.parser_for(conn.port).protocol_name == expected
    assert device.fw_banner != "old"
    assert device.firmware != "ghost-esp"


def test_probe_snapshot_is_deep_and_publishes_only_authoritative_fields(wired, monkeypatch):
    from src.core import handshake
    dm, hub, conn, device = wired
    device.telemetry = {"nested": {"latest": 1}}
    device.tags = ["original"]

    def probe(_conn, candidate, **_kwargs):
        assert candidate is not device and candidate.telemetry is not device.telemetry
        candidate.telemetry["nested"]["latest"] = -1
        candidate.tags.append("private")
        device.telemetry["nested"]["latest"] = 2
        device.arm_state = "safe"
        candidate.firmware = "ghost-esp"
        candidate.protocol = Protocol.GHOST_ESP
        candidate.health = "alive"
        candidate.fw_banner = "new banner"
        return HandshakeResult("alive", "new banner", frozenset({"help"}))

    monkeypatch.setattr(handshake, "probe_device", probe)
    result = dm.probe(conn.port)
    assert device.telemetry == {"nested": {"latest": 2}}
    assert device.tags == ["original"] and device.arm_state == "safe"
    assert (device.firmware, device.protocol, device.health, device.fw_banner) == (
        "ghost-esp", Protocol.GHOST_ESP, "alive", "new banner")
    assert result.live_commands == frozenset({"help"})
    assert hub.ingestor.parser_for(conn.port).protocol_name == "ghost-esp"


@pytest.mark.parametrize("binding", ["managed", "injected"])
@pytest.mark.parametrize("change", ["same-object-reconnect", "replacement-stale-callback"])
def test_probe_state_events_belong_to_current_connection(monkeypatch, binding, change):
    from src.core import device_manager, handshake
    from src.core.serial_handler import ConnectionState

    dm = DeviceManager()
    hub = CrossCommHub(dm)
    conn = InertConnection()
    device = Device(port=conn.port)
    dm.add_device(device)
    if binding == "managed":
        monkeypatch.setattr(device_manager, "SerialConnection", lambda *args, **kwargs: conn)
        monkeypatch.setattr(conn, "connect", lambda: None, raising=False)
        dm.open_connection(conn.port)
    else:
        dm.attach_connection(device, conn)
    old_state = conn.states[0]
    entered, resume = threading.Event(), threading.Event()

    def probe(_conn, candidate, **kwargs):
        entered.set()
        assert resume.wait(3)
        candidate.firmware = "ghost-esp"
        candidate.protocol = Protocol.GHOST_ESP
        candidate.health = "alive"
        return HandshakeResult("alive", "")

    if change == "replacement-stale-callback":
        device = Device(port=conn.port)
        current = InertConnection(conn.port)
        dm.attach_connection(device, current)
    monkeypatch.setattr(handshake, "probe_device", probe)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(dm.probe, conn.port)
            assert entered.wait(3)
            try:
                conn.is_connected = False
                old_state(ConnectionState.DISCONNECTED)
                if change == "same-object-reconnect":
                    assert not device.connected
                    conn.is_connected = True
                    old_state(ConnectionState.CONNECTED)
                assert device.connected
            finally:
                resume.set()
            result = pending.result(timeout=3)
        if change == "same-object-reconnect":
            assert result is None and device.firmware == ""
            assert hub.ingestor.parser_for(conn.port).protocol_name == "marauder"
        else:
            assert result is not None and device.firmware == "ghost-esp"
            assert hub.ingestor.parser_for(conn.port).protocol_name == "ghost-esp"
    finally:
        resume.set()
        hub.close()
