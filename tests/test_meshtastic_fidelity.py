"""Mesh source fidelity with synthetic protobuf and managed inert connections only."""
from __future__ import annotations

import threading

import pytest

from src.core.cross_comm_hub import CrossCommHub
from src.core.device_manager import DeviceManager
from src.models.device import Device
from src.protocols import meshtastic_proto as mp
from src.protocols.meshtastic_stream import MeshtasticBackend
from src.protocols.stream_framer import StreamFramer


def node(num=100):
    return StreamFramer.frame(mp.field_bytes(4, mp.field_varint(1, num)
                                            + mp.field_bytes(2, mp.field_bytes(2, b"Inert node"))))


def my_info(num=100):
    return StreamFramer.frame(mp.field_bytes(3, mp.field_varint(1, num)))


def channel():
    # Proto3 omits the primary channel's default index zero.
    return StreamFramer.frame(bytes.fromhex("520912051a034c61621801"))


def config():
    return StreamFramer.frame(bytes.fromhex("2a083206080110003801"))


def text():
    # FromRadio.packet {from:100, decoded:{portnum:TEXT_MESSAGE_APP, payload:"Inert"}}
    return StreamFramer.frame(bytes.fromhex("12100d64000000220908011205496e657274"))


@pytest.mark.parametrize('first_local', [True, False])
def test_local_marker_is_independent_of_arrival_order(first_local):
    backend = MeshtasticBackend(lambda _: None)
    chunks = (my_info(), node()) if first_local else (node(), my_info())
    backend.feed_bytes(b''.join(chunks) + node(200))
    assert [(n.num, n.is_local) for n in backend.node_list()] == [(100, True), (200, False)]
    backend.feed_bytes(my_info(200))
    assert [(n.num, n.is_local) for n in backend.node_list()] == [(200, True), (100, False)]
    backend.feed_bytes(my_info(mp.BROADCAST_NUM))
    assert backend.my_node_num is None
    assert all(not n.is_local for n in backend.node_list())


def test_all_legacy_views_and_coherent_snapshot_are_detached():
    events = []
    backend = MeshtasticBackend(lambda _: None, on_event=lambda name, data: events.append(data))
    backend.feed_bytes(node() + my_info() + channel() + config())
    snapshot = backend.snapshot()
    assert snapshot['session_id'] == backend.session_id
    assert snapshot['nodes'][0]['is_local'] is True
    snapshot['nodes'][0]['long_name'] = 'changed'
    snapshot['channels'][0]['name'] = 'changed'
    snapshot['lora_config']['region'] = 99
    backend.nodes[100].long_name = 'changed'
    backend.node_list()[0].long_name = 'changed'
    backend.channels[0].name = 'changed'
    backend.active_channels()[0].name = 'changed'
    backend.primary_channel().name = 'changed'
    backend.lora_config.region = 99
    events[0]['long_name'] = 'changed'
    assert backend.node_list()[0].long_name == 'Inert node'
    assert backend.primary_channel().name == 'Lab'
    assert backend.lora_config.region == 1
    assert {item['session_id'] for item in events} == {backend.session_id}
    assert MeshtasticBackend(lambda _: None).session_id != backend.session_id


class IntValue(int):
    pass


class TextValue(str):
    pass


@pytest.mark.parametrize('message', [None, b'Inert', TextValue('Inert'), '', ' \t\n', '\ud800',
    'x' * 234, '\u00e9' * 117, '\U0001f4e1' * 59])
def test_invalid_text_never_reaches_writer(message):
    writes = []
    backend = MeshtasticBackend(writes.append)
    with pytest.raises((TypeError, ValueError)):
        backend.send_text(message)
    assert writes == []


@pytest.mark.parametrize('arguments', [
    {'channel': -1}, {'channel': 8}, {'channel': 2**32}, {'channel': True}, {'channel': 1.0},
    {'channel': IntValue(1)}, {'dest': 0}, {'dest': 1}, {'dest': 2}, {'dest': 3},
    {'dest': -1}, {'dest': 2**32}, {'dest': True}, {'dest': 4.0}, {'dest': IntValue(4)},
])
def test_invalid_channel_or_destination_never_reaches_writer(arguments):
    writes = []
    backend = MeshtasticBackend(writes.append)
    with pytest.raises((TypeError, ValueError)):
        backend.send_text('Inert', **arguments)
    assert writes == []


@pytest.mark.parametrize('message', ['x' * 233, '\u00e9' * 116 + 'x', '\U0001f4e1' * 58 + 'x', '  Inert\n'])
@pytest.mark.parametrize('dest', [4, 255, 0x80000001, 0xFFFFFFFE, mp.BROADCAST_NUM])
def test_accepted_text_is_literal_single_write_with_full_unsigned_destination(message, dest):
    writes = []
    backend = MeshtasticBackend(writes.append)
    assert backend.send_text(message, channel=7, dest=dest) is None
    assert len(writes) == 1
    packet = mp.parse(mp.parse(StreamFramer().feed(writes[0])[0])[1][0])
    assert mp.as_u32(packet[2][0]) == dest
    assert packet[3][0] == 7
    assert mp.parse(packet[4][0])[2][0] == message.encode('utf-8')


def test_writer_failure_is_not_replayed_or_translated_to_success():
    error = OSError('inert writer failure')
    calls = []
    def writer(data):
        calls.append(data)
        raise error
    with pytest.raises(OSError) as raised:
        MeshtasticBackend(writer).send_text('Inert')
    assert raised.value is error and len(calls) == 1


@pytest.mark.parametrize('hex_payload', [
    '2001',                         # FromRadio.node_info has varint wire type
    '2200',                         # NodeInfo.num absent
    '22020800',                     # NodeInfo.num zero
    '2206088080808010',             # NodeInfo.num beyond uint32
    '22050d64000000',               # NodeInfo.num fixed32, not uint32 varint
    '22050864120278',               # incomplete nested User after valid node number
    '220508649a0601',               # incomplete unknown nested field
    '220408641001',                 # NodeInfo.user varint, not submessage
    '220508641a0100',               # malformed Position field number zero
    '1a030a0164',                   # MyNodeInfo.my_node_num wrong type
    '1a00',                         # local identity absent
    '12080864220408011200',         # MeshPacket.from wrong type
    '12070d640000002001',           # MeshPacket.decoded wrong type
    '12080d640000001a0100',         # MeshPacket.channel wrong type
    '120d0d6400000022060a0101120178', # Data.portnum wrong type
    '120b0d64000000220408011001',    # Data.payload wrong type
    '52030a0100',                   # Channel.index wrong type
    '52021001',                     # Channel.settings wrong type
    '2a023001',                     # Config.lora wrong type
    '3d01000000',                   # config_complete_id fixed32, not uint32
])
def test_malformed_identity_frames_cannot_mutate_state_or_publish(hex_payload):
    events = []
    backend = MeshtasticBackend(lambda _: None, on_event=lambda *args: events.append(args))
    backend.feed_bytes(StreamFramer.frame(bytes.fromhex(hex_payload)))
    assert backend.node_list() == [] and backend.active_channels() == []
    assert backend.my_node_num is None and backend.lora_config is None
    assert events == []


def test_valid_identity_unknown_fields_and_fixed32_timestamp_survive():
    # All supported unknown wire types remain skippable; last_heard is fixed32, not varint.
    unknown = bytes.fromhex('980601920601788d06010000008106') + b'\0' * 8
    payload = mp.field_bytes(4, bytes.fromhex('08642d7b000000') + unknown)
    result = mp.decode_fromradio(payload)
    assert result.node.num == 100 and result.node.last_heard == 123
    assert mp.decode_fromradio(mp.field_bytes(4, bytes.fromhex('0864287b'))).kind == 'other'


def test_omitted_local_origin_and_primary_channel_keep_proto3_defaults():
    result = mp.decode_fromradio(bytes.fromhex('120b220908011205496e657274'))
    assert result.kind == 'text' and result.text.from_num == 0 and result.text.channel == 0
    assert result.text.text == 'Inert'


def test_bad_frame_does_not_poison_following_valid_frame():
    backend = MeshtasticBackend(lambda _: None)
    backend.feed_bytes(StreamFramer.frame(bytes.fromhex('2001')) + node())
    assert [n.num for n in backend.node_list()] == [100]


class InertConnection:
    def __init__(self, port='INERT_MESH'):
        self.port, self.raw, self.is_connected = port, False, True
        self.bytes, self.lines, self.states, self.writes = [], [], [], []
    def on_bytes(self, cb): self.bytes.append(cb)
    def remove_byte_callback(self, cb):
        if cb in self.bytes: self.bytes.remove(cb)
    def on_line(self, cb): self.lines.append(cb)
    def remove_line_callback(self, cb):
        if cb in self.lines: self.lines.remove(cb)
    def on_state_change(self, cb): self.states.append(cb)
    def remove_state_callback(self, cb):
        if cb in self.states: self.states.remove(cb)
    def write_bytes(self, data): self.writes.append(data)
    def disconnect(self): self.is_connected = False
    def feed(self, data):
        for cb in tuple(self.bytes): cb(data)


@pytest.fixture
def managed(tmp_path):
    dm = DeviceManager()
    hub = CrossCommHub(dm, captures_persist_path=str(tmp_path / 'captures.json'))
    def attach(conn=None, device=None):
        conn = conn or InertConnection()
        dm.attach_connection(device or Device(conn.port, firmware='meshtastic'), conn, owner='inert')
        return conn
    try:
        yield dm, hub, attach
    finally:
        hub.close()


@pytest.mark.parametrize('replacement_kind', ['connection', 'device', 'firmware'])
def test_managed_replacement_retires_copied_source_and_assigns_new_session(managed, replacement_kind):
    dm, hub, attach = managed
    old = attach()
    backend, copied = old.mesh_backend, old.bytes[0]
    got = []
    hub.bus.subscribe('mesh.text', lambda topic, data: got.append(data))
    old.feed(text())
    assert len(got) == 1 and got[0]['session_id'] == backend.session_id
    if replacement_kind == 'connection':
        current = attach()
    elif replacement_kind == 'device':
        current = attach(old, Device(old.port, firmware='meshtastic'))
    else:
        current = old
        device = dm.get_device(old.port)
        device.firmware = 'marauder'
        hub._on_device_changed(device)
        device.firmware = 'meshtastic'
        hub._on_device_changed(device)
    assert current.mesh_backend.session_id != backend.session_id
    assert backend.snapshot()['retired'] is True
    copied(node(300) + text() + b'old log\n')
    assert backend.node_list() == [] and len(got) == 1
    current.feed(text())
    assert len(got) == 2 and got[-1]['session_id'] == current.mesh_backend.session_id


def test_retirement_during_decode_blocks_later_publication(managed, monkeypatch):
    _, hub, attach = managed
    old = attach()
    backend = old.mesh_backend
    got = []
    hub.bus.subscribe('mesh.text', lambda topic, data: got.append(data))
    decode = mp.decode_fromradio
    def replace_during_decode(payload):
        result = decode(payload)
        attach()
        return result
    monkeypatch.setattr(mp, 'decode_fromradio', replace_during_decode)
    old.feed(text())
    assert backend.snapshot()['retired'] and got == []


def test_admitted_publication_retains_old_token_and_runs_outside_source_locks(managed, monkeypatch):
    _, hub, attach = managed
    old = attach()
    backend = old.mesh_backend
    got, failures = [], []
    hub.bus.subscribe('mesh.text', lambda topic, data: got.append(data))
    publish = hub.bus.publish
    def paused_publication(topic, data):
        if topic == 'mesh.text':
            def replace():
                try:
                    backend.snapshot()
                    attach()
                except BaseException as exc:
                    failures.append(exc)
            worker = threading.Thread(target=replace)
            worker.start()
            worker.join(3)
            assert not worker.is_alive(), 'publication held a lock needed by snapshot/replacement'
        publish(topic, data)
    monkeypatch.setattr(hub.bus, 'publish', paused_publication)
    old.feed(text())
    assert not failures and len(got) == 1
    assert got[0]['session_id'] == backend.session_id
    assert got[0]['session_id'] != hub.mesh_backend(old.port).session_id


def test_debug_events_are_session_tagged_and_closed_scope_is_inert(managed):
    _, hub, attach = managed
    conn = attach()
    backend, copied = conn.mesh_backend, conn.bytes[0]
    got = []
    hub.bus.subscribe('mesh.log', lambda topic, data: got.append(data))
    conn.feed(b'Inert log\n')
    assert got == [{'port': conn.port, 'session_id': backend.session_id, 'line': 'Inert log'}]
    hub.close()
    copied(node() + text() + b'Retired log\n')
    assert len(got) == 1 and backend.node_list() == []


def test_foreign_backend_replacement_disables_old_source_without_clearing_new_owner(managed):
    _, hub, attach = managed
    conn = attach()
    backend, copied = conn.mesh_backend, conn.bytes[0]
    replacement = object()
    conn.mesh_backend = replacement
    copied(node())
    assert backend.node_list() == []
    hub.close()
    assert conn.mesh_backend is replacement and conn.raw is True
