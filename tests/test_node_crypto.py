"""Tests for node_crypto (W1.0) — the AEAD wire frame + anti-replay for the wireless NodeLink.

Crypto correctness is the whole point, so these lean hard on the guarantees: round-trip, tamper /
forgery rejection, nonce uniqueness (reuse impossible), epoch rotation, and the sliding-window
replay guard's edge cases. All keys here are OBVIOUSLY FAKE test constants — no real secret material.
"""
from __future__ import annotations

import struct

import pytest

from src.core import node_crypto as nc
from src.core.node_crypto import (
    HEADER_LEN,
    OVERHEAD,
    TAG_LEN,
    AuthenticationError,
    Frame,
    FrameSealer,
    NonceExhaustedError,
    ReplayError,
    ReplayWindow,
    max_plaintext,
    seal,
    unseal,
)

# Obviously-fake, deterministic test keys — NOT real key material.
KEY = bytes(32)                 # all zeros
KEY2 = bytes([0x11]) * 32       # a different fake key
MAX_U32 = (1 << 32) - 1
MAX_U64 = (1 << 64) - 1


# ── Format / constants ───────────────────────────────────────────────
def test_overhead_and_mtu_math():
    assert HEADER_LEN == 15 and TAG_LEN == 16 and OVERHEAD == 31
    assert max_plaintext(250) == 219       # ESP-NOW single-frame budget
    assert max_plaintext(31) == 0 and max_plaintext(10) == 0


def test_wire_header_is_exactly_the_spec():
    wire = seal(KEY, node_id=0x1234, epoch=7, counter=42, plaintext=b"hi")
    # version(1) | node_id(2) | epoch(4) | counter(8), big-endian — locks the on-wire format.
    assert wire[:HEADER_LEN] == struct.pack(">BHIQ", nc.VERSION, 0x1234, 7, 42)
    assert len(wire) == HEADER_LEN + len(b"hi") + TAG_LEN


# ── Round-trip ───────────────────────────────────────────────────────
@pytest.mark.parametrize("pt", [b"", b"x", b"hello node", bytes(range(256)), b"\x00" * 219])
def test_round_trip(pt):
    wire = seal(KEY, node_id=1, epoch=0, counter=5, plaintext=pt)
    frame, out = unseal(KEY, wire)
    assert out == pt
    assert frame == Frame(version=nc.VERSION, node_id=1, epoch=0, counter=5)


def test_round_trip_with_aad():
    wire = seal(KEY, node_id=1, epoch=0, counter=0, plaintext=b"payload", aad=b"ctx-A")
    _, out = unseal(KEY, wire, aad=b"ctx-A")
    assert out == b"payload"


# ── Tamper / forgery rejection ───────────────────────────────────────
def test_wrong_key_rejected():
    wire = seal(KEY, node_id=1, epoch=0, counter=0, plaintext=b"secret")
    with pytest.raises(AuthenticationError):
        unseal(KEY2, wire)


def test_wrong_aad_rejected():
    wire = seal(KEY, node_id=1, epoch=0, counter=0, plaintext=b"p", aad=b"ctx-A")
    with pytest.raises(AuthenticationError):
        unseal(KEY, wire, aad=b"ctx-B")
    with pytest.raises(AuthenticationError):
        unseal(KEY, wire)  # missing aad


@pytest.mark.parametrize("flip_index", [0, 3, 8, 14, HEADER_LEN, HEADER_LEN + 2, -1])
def test_single_bit_flip_anywhere_rejected(flip_index):
    """Flipping any byte — header (authenticated as AAD) or ciphertext/tag — must fail the tag."""
    wire = bytearray(seal(KEY, node_id=1, epoch=2, counter=9, plaintext=b"important"))
    wire[flip_index] ^= 0x01
    with pytest.raises(AuthenticationError):
        unseal(KEY, bytes(wire))


@pytest.mark.parametrize("truncated", [b"", b"\x00" * 10, b"\x00" * (HEADER_LEN + TAG_LEN - 1)])
def test_truncated_frame_rejected(truncated):
    with pytest.raises(AuthenticationError):
        unseal(KEY, truncated)


# ── Input validation ─────────────────────────────────────────────────
@pytest.mark.parametrize("badkey", [b"", bytes(16), bytes(31), bytes(33), "not-bytes"])
def test_bad_key_length_rejected(badkey):
    with pytest.raises((ValueError, TypeError)):
        seal(badkey, node_id=1, epoch=0, counter=0, plaintext=b"x")


@pytest.mark.parametrize(
    "node_id,epoch,counter",
    [(1 << 16, 0, 0), (0, 1 << 32, 0), (0, 0, 1 << 64), (-1, 0, 0), (0, -1, 0)],
)
def test_out_of_range_fields_rejected(node_id, epoch, counter):
    with pytest.raises(ValueError):
        seal(KEY, node_id=node_id, epoch=epoch, counter=counter, plaintext=b"x")


# ── Nonce uniqueness / sealer state ──────────────────────────────────
def test_sealer_counter_is_monotonic_and_unique():
    s = FrameSealer(KEY, node_id=7)
    nonces = set()
    for expected in range(1000):
        wire = s.seal(b"tick")
        _, nid, epoch, counter = struct.unpack(">BHIQ", wire[:HEADER_LEN])
        assert nid == 7 and epoch == 0 and counter == expected
        nonces.add((epoch, counter))
    assert len(nonces) == 1000  # never a repeated nonce


def test_sealer_rotates_epoch_on_counter_overflow():
    s = FrameSealer(KEY, node_id=1, epoch=4, counter=MAX_U64)
    w1 = s.seal(b"a")   # uses counter == MAX_U64
    w2 = s.seal(b"b")   # overflow -> epoch 5, counter 0
    _, _, e1, c1 = struct.unpack(">BHIQ", w1[:HEADER_LEN])
    _, _, e2, c2 = struct.unpack(">BHIQ", w2[:HEADER_LEN])
    assert (e1, c1) == (4, MAX_U64)
    assert (e2, c2) == (5, 0)


def test_sealer_raises_when_nonce_space_exhausted():
    s = FrameSealer(KEY, node_id=1, epoch=MAX_U32, counter=MAX_U64)
    s.seal(b"last")  # consumes the final nonce
    with pytest.raises(NonceExhaustedError):
        s.seal(b"one too many")


# ── Replay window edge cases ─────────────────────────────────────────
def test_replay_in_order_accepts_and_rejects_duplicates():
    w = ReplayWindow(window_size=64)
    for c in range(100):
        assert w.check_and_update(0, c) is True
    # Every one of those is now a duplicate (well within a 64 window for the last 64) or too-old.
    assert w.check_and_update(0, 99) is False   # duplicate (head)
    assert w.check_and_update(0, 90) is False   # duplicate (in window)


def test_replay_reorder_within_window():
    w = ReplayWindow(window_size=32)
    assert w.check_and_update(0, 10) is True
    assert w.check_and_update(0, 5) is True     # older but inside the window -> accept once
    assert w.check_and_update(0, 5) is False    # ...then a duplicate
    assert w.check_and_update(0, 8) is True
    assert w.check_and_update(0, 10) is False   # head duplicate


def test_replay_too_old_rejected():
    w = ReplayWindow(window_size=16)
    assert w.check_and_update(0, 100) is True
    assert w.check_and_update(0, 100 - 16) is False  # exactly at/over the edge -> too old
    assert w.check_and_update(0, 50) is False        # way old
    assert w.check_and_update(0, 100 - 15) is True   # just inside the window


def test_replay_large_jump_forward():
    w = ReplayWindow(window_size=8)
    assert w.check_and_update(0, 1) is True
    assert w.check_and_update(0, 1000) is True   # jump >> window; old bits roll off
    assert w.check_and_update(0, 1) is False     # now far too old
    assert w.check_and_update(0, 1000) is False  # duplicate head


def test_replay_epoch_reset_and_stale_epoch():
    w = ReplayWindow(window_size=16)
    assert w.check_and_update(3, 50) is True
    assert w.check_and_update(3, 50) is False    # dup in epoch 3
    assert w.check_and_update(4, 0) is True       # newer epoch resets the window
    assert w.check_and_update(4, 0) is False      # dup in epoch 4
    assert w.check_and_update(3, 999) is False    # stale epoch rejected even with a high counter


@pytest.mark.parametrize("bad", [(-1, 0), (0, -1), (True, 0), (0, False), ("a", 0)])
def test_replay_rejects_invalid_inputs(bad):
    w = ReplayWindow(window_size=8)
    assert w.check_and_update(*bad) is False


def test_replay_window_size_validation():
    for bad in (0, -1, "x"):
        with pytest.raises(ValueError):
            ReplayWindow(window_size=bad)


# ── unseal + replay integration ──────────────────────────────────────
def test_unseal_with_replay_rejects_replayed_frame():
    w = ReplayWindow(window_size=64)
    wire = seal(KEY, node_id=1, epoch=0, counter=3, plaintext=b"once")
    _, out = unseal(KEY, wire, replay=w)      # first sight -> ok
    assert out == b"once"
    with pytest.raises(ReplayError):           # exact same bytes again -> replay
        unseal(KEY, wire, replay=w)


def test_forged_frame_never_poisons_the_replay_window():
    w = ReplayWindow(window_size=64)
    good = seal(KEY, node_id=1, epoch=0, counter=5, plaintext=b"real")
    unseal(KEY, good, replay=w)
    assert w.highest == 5

    # A forgery claiming a far-future counter must be rejected on AUTH, before the window is touched.
    forged = bytearray(seal(KEY2, node_id=1, epoch=0, counter=9999, plaintext=b"fake"))
    with pytest.raises(AuthenticationError):
        unseal(KEY, bytes(forged), replay=w)
    assert w.highest == 5  # window unchanged -> forgery could not advance it

    # And the legitimate next frame still flows.
    nxt = seal(KEY, node_id=1, epoch=0, counter=6, plaintext=b"next")
    _, out = unseal(KEY, nxt, replay=w)
    assert out == b"next" and w.highest == 6


def test_auth_is_checked_before_replay():
    """A frame that is BOTH a replay and tampered must report AuthenticationError (auth is first)."""
    w = ReplayWindow(window_size=64)
    wire = seal(KEY, node_id=1, epoch=0, counter=1, plaintext=b"x")
    unseal(KEY, wire, replay=w)
    tampered = bytearray(wire)
    tampered[-1] ^= 0x01
    with pytest.raises(AuthenticationError):
        unseal(KEY, bytes(tampered), replay=w)


# ── End-to-end sender+receiver ───────────────────────────────────────
def test_end_to_end_sealer_and_window():
    s = FrameSealer(KEY, node_id=42)
    w = ReplayWindow(window_size=128)
    frames = [s.seal(f"msg {i}".encode()) for i in range(200)]
    for i, wire in enumerate(frames):
        frame, out = unseal(KEY, wire, replay=w)
        assert out == f"msg {i}".encode() and frame.node_id == 42
    # Replaying any earlier frame that is still... too old now (window 128, head 199) -> rejected.
    with pytest.raises((ReplayError, AuthenticationError)):
        unseal(KEY, frames[0], replay=w)
    # A recent one within the window is a duplicate.
    with pytest.raises(ReplayError):
        unseal(KEY, frames[199], replay=w)


# ── Regression / property tests (from the crypto DEBUG review) ───────
def test_replay_huge_counter_jump_is_bounded():
    """Regression for the unbounded-shift hazard: an enormous authentic counter jump must resolve
    with a bitmap bounded to the window — not allocate a giant integer via `bitmap << shift`."""
    w = ReplayWindow(window_size=64)
    assert w.check_and_update(0, 1) is True
    assert w.check_and_update(0, 10**15) is True          # enormous in-epoch jump
    assert w._bitmap.bit_length() <= 64                    # stayed within the window (no blowup)
    assert w.check_and_update(0, 10**15) is False          # duplicate head
    assert w.check_and_update(0, 1) is False               # now far too old
    assert w.check_and_update(0, 10**15 - 10) is True      # just inside the window after the jump


def test_sealer_nonces_unique_across_epoch_boundary():
    """Property: sweeping seals across a forced epoch rotation, every nonce stays distinct."""
    s = FrameSealer(KEY, node_id=3, epoch=0, counter=MAX_U64 - 500)
    nonces = set()
    for _ in range(1000):  # straddles the counter overflow -> epoch 0 then 1
        _, _, epoch, counter = struct.unpack(">BHIQ", s.seal(b"x")[:HEADER_LEN])
        nonces.add((epoch, counter))
    assert len(nonces) == 1000                     # no repeat across the boundary
    assert {e for (e, _) in nonces} == {0, 1}      # confirms the boundary was actually crossed


def test_sealer_persist_restore_does_not_reuse_nonce():
    """The persistence contract: saving .epoch/.counter (next-to-use) and resuming reuses no nonce."""
    s1 = FrameSealer(KEY, node_id=9)
    used = set()
    for _ in range(10):
        _, _, e, c = struct.unpack(">BHIQ", s1.seal(b"a")[:HEADER_LEN])
        used.add((e, c))
    s2 = FrameSealer(KEY, node_id=9, epoch=s1.epoch, counter=s1.counter)  # resume from persisted state
    for _ in range(10):
        _, _, e, c = struct.unpack(">BHIQ", s2.seal(b"b")[:HEADER_LEN])
        assert (e, c) not in used                  # never a reused nonce across the restart
        used.add((e, c))
    assert len(used) == 20


def test_unseal_surfaces_version_for_caller_gating():
    """unseal returns the authenticated version; it does not itself reject an unknown one."""
    wire = seal(KEY, node_id=1, epoch=0, counter=0, plaintext=b"x", version=2)
    frame, out = unseal(KEY, wire)
    assert frame.version == 2 and out == b"x"
