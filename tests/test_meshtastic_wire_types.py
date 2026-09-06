"""Independent raw-wire tests: no production encoder constructs input fields."""
import struct

import pytest

from src.protocols import meshtastic_proto as mp
from src.protocols.meshtastic_stream import MeshtasticBackend
from src.protocols.stream_framer import StreamFramer


def varint(value):
    value &= (1 << 64) - 1
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def field(number, wire, value):
    tag = varint((number << 3) | wire)
    if wire == 0:
        return tag + varint(value)
    if wire == 2:
        return tag + varint(len(value)) + value
    return tag + value


def radio(kind, body):
    return field(kind, 2, body)


def with_float(location, wire, value):
    if location == 'node_snr':
        return radio(4, b'\x08\x64' + field(4, wire, value))
    if location == 'rx_snr':
        return radio(2, bytes.fromhex('0d6400000022050801120178') + field(8, wire, value))
    number = {'voltage': 2, 'channel_util': 3, 'air_util_tx': 4}[location]
    return radio(4, b'\x08\x64' + field(6, 2, field(number, wire, value)))


@pytest.mark.parametrize('location', ['node_snr', 'voltage', 'channel_util', 'air_util_tx', 'rx_snr'])
@pytest.mark.parametrize('wire,value', [(0, 7), (2, struct.pack('<f', 7.0))])
def test_wrong_wire_consumed_float_rejected_before_state_or_event(location, wire, value):
    payload = with_float(location, wire, value)
    result = mp.decode_fromradio(payload)
    events = []
    backend = MeshtasticBackend(lambda _: None, on_event=lambda *event: events.append(event))
    backend.feed_bytes(StreamFramer.frame(payload))
    assert result.kind == 'other', (location, wire, result)
    assert backend.snapshot()['nodes'] == [] and events == []


@pytest.mark.parametrize('location', ['node_snr', 'voltage', 'channel_util', 'air_util_tx', 'rx_snr'])
@pytest.mark.parametrize('value', [0.0, -7.5])
def test_real_fixed32_float_preserves_numeric_value(location, value):
    result = mp.decode_fromradio(with_float(location, 5, struct.pack('<f', value)))
    if location == 'rx_snr':
        assert result.kind == 'text' and result.text.rx_snr == value
    else:
        name = 'snr' if location == 'node_snr' else location
        assert result.kind == 'node_info' and getattr(result.node, name) == value


@pytest.mark.parametrize('value', [-1, -2, -(1 << 31)])
def test_signed_channel_index_stays_negative_and_is_not_an_active_channel(value):
    payload = radio(10, field(1, 0, value) + b'\x18\x01')
    result = mp.decode_fromradio(payload)
    events = []
    backend = MeshtasticBackend(lambda _: None, on_event=lambda *event: events.append(event))
    backend.feed_bytes(StreamFramer.frame(payload))
    assert backend.active_channels() == [] and backend.snapshot()['channels'] == []
    assert events == []
    assert result.kind == 'channel' and result.channel.index == value


@pytest.mark.parametrize('index', [None, 0, 7])
def test_valid_channel_default_and_edge_are_preserved(index):
    body = (b'' if index is None else field(1, 0, index)) + b'\x18\x01'
    result = mp.decode_fromradio(radio(10, body))
    assert result.kind == 'channel' and result.channel.index == (index or 0)


@pytest.mark.parametrize('wire', [2, 5])
def test_wrong_wire_consumed_rssi_rejects_complete_packet(wire):
    body = bytes.fromhex('0d6400000022050801120178') + field(12, wire, struct.pack('<i', -95))
    assert mp.decode_fromradio(radio(2, body)).kind == 'other'


def test_real_signed_rssi_remains_negative_with_unknown_fields():
    body = bytes.fromhex('0d6400000022050801120178') + field(12, 0, -95) + field(100, 2, b'future')
    result = mp.decode_fromradio(radio(2, body))
    assert result.kind == 'text' and result.text.rx_rssi == -95
