"""Verify the relay firmware's protocol contract against the real host crypto (W1.0).

The relay sketch (firmware/relay/relay.ino) can't be compiled here, but it is a pure byte transcoder:
host→node it base64-DECODES a serial line into the raw frame it broadcasts over ESP-NOW; node→host it
base64-ENCODES a raw ESP-NOW frame back into a serial line. These tests model exactly those transforms
with the real node_crypto seal/unseal, proving the transcode is lossless and the protocol invariants the
firmware leans on (fits the ESP-NOW MTU, broadcast-downlink is safe) actually hold.
"""
import base64

import pytest

from src.core import node_crypto as nc

KEY = bytes(range(32))          # a 32-byte per-node key
NODE_ID = 7


def test_downlink_host_line_decodes_to_a_frame_the_node_unseals():
    sealer = nc.FrameSealer(KEY, NODE_ID)
    frame = sealer.seal(b"scanap")                       # host seals a command...
    host_line = base64.b64encode(frame).decode("ascii")  # ...and base64-lines it (NodeLink._send_sealed)

    raw = base64.b64decode(host_line, validate=True)      # RELAY: line -> raw frame for ESP-NOW
    assert raw == frame
    assert len(raw) <= nc.ESP_NOW_MTU                     # fits a single ESP-NOW frame

    node_frame, plaintext = nc.unseal(KEY, raw, replay=nc.ReplayWindow())  # NODE unseals
    assert plaintext == b"scanap" and node_frame.node_id == NODE_ID


def test_uplink_node_frame_encodes_to_a_line_the_host_unseals():
    sealer = nc.FrameSealer(KEY, NODE_ID)
    frame = sealer.seal(b"AP found: 00:11:22:33:44:55")   # node seals its firmware output

    host_line = base64.b64encode(frame).decode("ascii")   # RELAY: raw ESP-NOW frame -> host serial line
    raw = base64.b64decode(host_line, validate=True)       # HOST (NodeLink._on_gateway_line)
    _, plaintext = nc.unseal(KEY, raw, replay=nc.ReplayWindow())
    assert plaintext == b"AP found: 00:11:22:33:44:55"


def test_broadcast_downlink_is_safe_wrong_key_drops():
    # The relay broadcasts every downlink; a node holding a different key must NOT be able to read it.
    frame = nc.FrameSealer(KEY, NODE_ID).seal(b"reboot")
    other_key = bytes([0xAA]) * 32
    with pytest.raises(nc.AuthenticationError):
        nc.unseal(other_key, frame, replay=nc.ReplayWindow())


def test_max_plaintext_frame_exactly_fills_the_espnow_budget():
    pt = b"x" * nc.max_plaintext(nc.ESP_NOW_MTU)
    frame = nc.FrameSealer(KEY, NODE_ID).seal(pt)
    assert len(frame) == nc.ESP_NOW_MTU                   # largest single-frame payload fits, no fragmentation
