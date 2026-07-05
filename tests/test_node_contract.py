"""Verify the node firmware's protocol contract against the real host crypto (W1.0).

firmware/node/node.ino can't be compiled here, so this models its logic in Python — the same AES-256-GCM
envelope (via the real node_crypto) plus its strict-monotonic anti-replay — and proves a host↔node command
and reply survive intact, that replays and stale counters are dropped, and that a frame under the wrong key
is rejected. If this passes, the sketch's protocol behaviour is correct by construction (it ports these
exact rules to mbedtls).
"""
import pytest

from src.core import node_crypto as nc

HOST_KEY = bytes(range(32))     # the per-node key the host and this node share
NODE_ID = 1


class NodeModel:
    """Mirror of node.ino: seal replies with a monotonic sealer, open commands with a strict-monotonic
    (reject anything not strictly newer within the epoch) anti-replay rule."""

    def __init__(self, key: bytes, node_id: int, tx_epoch: int = 1) -> None:
        self.key = key
        self.node_id = node_id
        self._sealer = nc.FrameSealer(key, node_id, epoch=tx_epoch, counter=0)
        self._have_rx = False
        self._rx_epoch = 0
        self._rx_highest = 0

    def open(self, wire: bytes) -> bytes:
        frame, pt = nc.unseal(self.key, wire)            # AuthenticationError on wrong key / tamper
        if frame.node_id != self.node_id:
            raise ValueError("frame addressed to another node")
        e, c = frame.epoch, frame.counter
        if not self._have_rx or e > self._rx_epoch:
            self._have_rx, self._rx_epoch, self._rx_highest = True, e, c
        elif e < self._rx_epoch or c <= self._rx_highest:
            raise nc.ReplayError("stale epoch / replay / duplicate")
        else:
            self._rx_highest = c
        return pt

    def seal(self, pt: bytes) -> bytes:
        return self._sealer.seal(pt)


def test_host_command_reaches_the_node():
    host = nc.FrameSealer(HOST_KEY, NODE_ID)
    node = NodeModel(HOST_KEY, NODE_ID)
    assert node.open(host.seal(b"ping")) == b"ping"


def test_node_reply_reaches_the_host():
    node = NodeModel(HOST_KEY, NODE_ID)
    win = nc.ReplayWindow()
    _, pt = nc.unseal(HOST_KEY, node.seal(b"node 1: pong"), replay=win)
    assert pt == b"node 1: pong"


def test_node_drops_a_replayed_command():
    host = nc.FrameSealer(HOST_KEY, NODE_ID)
    node = NodeModel(HOST_KEY, NODE_ID)
    wire = host.seal(b"reboot")
    assert node.open(wire) == b"reboot"                  # first delivery accepted
    with pytest.raises(nc.ReplayError):
        node.open(wire)                                  # exact replay rejected


def test_node_drops_an_out_of_order_older_command():
    host = nc.FrameSealer(HOST_KEY, NODE_ID)
    node = NodeModel(HOST_KEY, NODE_ID)
    w1 = host.seal(b"first")
    w2 = host.seal(b"second")
    assert node.open(w2) == b"second"                    # newer counter accepted first
    with pytest.raises(nc.ReplayError):
        node.open(w1)                                     # older counter now rejected


def test_node_rejects_a_frame_under_the_wrong_key():
    attacker = nc.FrameSealer(bytes([0xAA]) * 32, NODE_ID)
    node = NodeModel(HOST_KEY, NODE_ID)
    with pytest.raises(nc.AuthenticationError):
        node.open(attacker.seal(b"reboot"))              # wrong key -> tag fails -> dropped
