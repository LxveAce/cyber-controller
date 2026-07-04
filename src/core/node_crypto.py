r"""Node crypto — the authenticated wire frame for the wireless NodeLink transport (W1.0).

This is the *host-side*, hardware-free foundation of wireless control: an AEAD (authenticated
encryption) frame plus anti-replay, so a controller and a remote CC node can exchange serial
traffic over an untrusted link (a USB gateway dongle now, ESP-NOW/BLE later) with confidentiality,
integrity, and replay protection. It is a pure library — no I/O, no UI, no key storage. NodeLink
(W1.0, later) will `seal()` on `write()` and `unseal()` incoming frames, riding the exact same
transport seam as ``SerialConnection`` (``write_bytes`` / ``on_line``).

Wire format (big-endian), matching the roadmap spec
``version | node_id | epoch(u32) | counter(u64) | AES-256-GCM(ciphertext‖tag)``::

    +---------+-----------+-----------+-------------+------------------------+
    | ver u8  | node_id u16| epoch u32 | counter u64 | AES-256-GCM(ct + 16B tag)|
    +---------+-----------+-----------+-------------+------------------------+
     \__________________ 15-byte header (authenticated as AAD) ____________/

Crypto choices (deliberate, not hand-rolled):
  * **AES-256-GCM** from ``cryptography`` (a vetted primitive). 32-byte per-node key, 16-byte tag.
  * **Nonce = epoch(4) ‖ counter(8) = 96 bits** — exactly the GCM nonce width. GCM's one hard rule
    is *never reuse a (key, nonce) pair*; :class:`FrameSealer` guarantees that **by construction**
    with a monotonic counter that rotates the epoch on overflow, so a nonce is never repeated under
    a key. The 96 bits are fully spent on epoch‖counter, so ``node_id`` is authenticated but *not*
    part of the nonce — which means **each node MUST hold its own key**. Sharing one key across two
    nodes would reuse nonces and break GCM; per-node provisioning (W1.1) is what makes it safe. Keys
    are provisioned host-side and never travel on air — this module only *uses* a key you hand it; it
    neither generates, derives, nor stores secrets.
  * The **whole 15-byte header is authenticated** as GCM associated data, so version/node_id/epoch/
    counter cannot be tampered without failing the tag. Callers may bind extra context via ``aad``.
  * **Anti-replay** (:class:`ReplayWindow`) is an IPsec/DTLS-style sliding bitmap window, checked
    **only after** the tag verifies — a forged frame can never poison the window.

MTU note: header(15) + tag(16) = **31 bytes** of overhead. ESP-NOW's payload MTU is 250 bytes, so a
single node frame carries up to ``max_plaintext(250)`` = 219 bytes; NodeLink fragments above that.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# --- Frame constants ---
VERSION = 1
KEY_LEN = 32           # AES-256
TAG_LEN = 16           # GCM tag
_HEADER_FMT = ">BHIQ"  # version u8 | node_id u16 | epoch u32 | counter u64
HEADER_LEN = struct.calcsize(_HEADER_FMT)  # 15
OVERHEAD = HEADER_LEN + TAG_LEN            # 31 bytes on top of the plaintext

ESP_NOW_MTU = 250      # ESP-NOW single-frame payload budget (roadmap); NodeLink fragments above this.

_MAX_U16 = (1 << 16) - 1
_MAX_U32 = (1 << 32) - 1
_MAX_U64 = (1 << 64) - 1


# --- Errors ---
class NodeCryptoError(Exception):
    """Base class for node-crypto failures."""


class AuthenticationError(NodeCryptoError):
    """The frame's tag did not verify — tampered, forged, wrong key, or truncated. Drop it."""


class ReplayError(NodeCryptoError):
    """The frame authenticated but is a replay (duplicate) or older than the replay window."""


class NonceExhaustedError(NodeCryptoError):
    """The (epoch, counter) nonce space for this key is exhausted — the key must be rotated.

    Astronomically far off in practice (2**32 epochs x 2**64 counters), but guarded so nonce
    reuse can never happen silently.
    """


@dataclass(frozen=True)
class Frame:
    """A parsed node frame header (the ciphertext is returned separately as the plaintext)."""

    version: int
    node_id: int
    epoch: int
    counter: int


def max_plaintext(mtu: int = ESP_NOW_MTU) -> int:
    """Largest plaintext that fits in a single transport frame of *mtu* bytes."""
    return max(0, mtu - OVERHEAD)


# --- Validation helpers ---
def _check_key(key: bytes) -> None:
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_LEN:
        raise ValueError(f"key must be exactly {KEY_LEN} bytes (AES-256)")


def _check_field(name: str, value: int, hi: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > hi:
        raise ValueError(f"{name} must be an int in [0, {hi}]")


def _nonce(epoch: int, counter: int) -> bytes:
    # 96-bit GCM nonce = epoch(u32) ‖ counter(u64). Unique per frame because the counter is monotonic.
    return struct.pack(">IQ", epoch, counter)


# --- Low-level frame codec ---
def seal(
    key: bytes,
    node_id: int,
    epoch: int,
    counter: int,
    plaintext: bytes,
    aad: bytes = b"",
    version: int = VERSION,
) -> bytes:
    """Encrypt+authenticate *plaintext* into a wire frame. Low-level: the caller is responsible for
    supplying a **unique** (epoch, counter) per key — use :class:`FrameSealer` to get that for free.

    *aad* is extra associated data bound into the tag alongside the header (both ends must agree).
    """
    _check_key(key)
    _check_field("node_id", node_id, _MAX_U16)
    _check_field("epoch", epoch, _MAX_U32)
    _check_field("counter", counter, _MAX_U64)
    _check_field("version", version, 0xFF)
    if not isinstance(plaintext, (bytes, bytearray)):
        raise TypeError("plaintext must be bytes")
    if not isinstance(aad, (bytes, bytearray)):
        raise TypeError("aad must be bytes")

    header = struct.pack(_HEADER_FMT, version, node_id, epoch, counter)
    # Authenticate the whole header + caller aad; the nonce is epoch‖counter (a slice of the header).
    ct = AESGCM(bytes(key)).encrypt(_nonce(epoch, counter), bytes(plaintext), bytes(header) + bytes(aad))
    return header + ct


def unseal(
    key: bytes,
    wire: bytes,
    replay: "ReplayWindow | None" = None,
    aad: bytes = b"",
) -> "tuple[Frame, bytes]":
    """Parse, verify, and decrypt a wire frame. Returns ``(Frame, plaintext)``.

    Raises :class:`AuthenticationError` if the frame is malformed or its tag does not verify (checked
    first, always). If a *replay* window is supplied, the authenticated frame is then checked against
    it and :class:`ReplayError` is raised for a duplicate or too-old frame — the window is only
    touched **after** a successful tag verify, so forged frames cannot poison it.

    ``frame.version`` is authenticated (it can't be tampered) but is **not** gated here — a frame
    sealed under a different wire version still decrypts. Callers that support multiple versions
    should check ``frame.version`` and route accordingly.
    """
    _check_key(key)
    if not isinstance(wire, (bytes, bytearray)):
        raise TypeError("wire must be bytes")
    if len(wire) < HEADER_LEN + TAG_LEN:
        # Too short to even hold a header + tag -> treat as a forgery/truncation, not a crash.
        raise AuthenticationError("frame shorter than header + tag")

    header = bytes(wire[:HEADER_LEN])
    version, node_id, epoch, counter = struct.unpack(_HEADER_FMT, header)
    ct = bytes(wire[HEADER_LEN:])
    if not isinstance(aad, (bytes, bytearray)):
        raise TypeError("aad must be bytes")

    try:
        plaintext = AESGCM(bytes(key)).decrypt(_nonce(epoch, counter), ct, header + bytes(aad))
    except InvalidTag as exc:
        raise AuthenticationError("frame failed authentication (tampered/forged/wrong key)") from exc

    # Authentic. Now (and only now) enforce anti-replay.
    if replay is not None and not replay.check_and_update(epoch, counter):
        raise ReplayError(f"replayed or stale frame (epoch={epoch}, counter={counter})")

    return Frame(version=version, node_id=node_id, epoch=epoch, counter=counter), plaintext


# --- Sender: monotonic nonce, reuse impossible by construction ---
class FrameSealer:
    """Stateful sender for one (key, node_id). Hands out a **unique** (epoch, counter) per frame, so
    a nonce is never reused under the key. The counter increments each :meth:`seal`; when it would
    exceed 2**64-1 the epoch rotates and the counter resets, and if the epoch is also exhausted a
    :class:`NonceExhaustedError` is raised (rotate the key). Not thread-safe — one sealer per sender.

    **Persistence contract:** ``.epoch`` and ``.counter`` always expose the *next* nonce to be used
    (the counter increments after a successful seal). To resume across a restart WITHOUT reusing a
    nonce, persist these two values verbatim and pass them back to ``__init__`` — never persist the
    *last-used* counter. When in doubt, bump the epoch on restart to skip past any in-flight counters.
    """

    def __init__(self, key: bytes, node_id: int, epoch: int = 0, counter: int = 0) -> None:
        _check_key(key)
        _check_field("node_id", node_id, _MAX_U16)
        _check_field("epoch", epoch, _MAX_U32)
        _check_field("counter", counter, _MAX_U64)
        self._key = bytes(key)
        self._node_id = node_id
        self._epoch = epoch
        self._counter = counter

    @property
    def node_id(self) -> int:
        return self._node_id

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def counter(self) -> int:
        return self._counter

    def seal(self, plaintext: bytes, aad: bytes = b"") -> bytes:
        """Seal *plaintext* with the next fresh nonce. Rotates the epoch on counter overflow."""
        if self._counter > _MAX_U64:
            if self._epoch >= _MAX_U32:
                raise NonceExhaustedError("nonce space exhausted for this key — rotate the key")
            self._epoch += 1
            self._counter = 0
        wire = seal(self._key, self._node_id, self._epoch, self._counter, plaintext, aad)
        self._counter += 1
        return wire


# --- Receiver: sliding-window anti-replay (IPsec/DTLS style) ---
class ReplayWindow:
    """Sliding-window replay guard for one sender. Accepts each authentic (epoch, counter) at most
    once. A strictly newer epoch resets the window (key rotation / sender restart); an older epoch is
    rejected. Within an epoch, a counter newer than the window head advances it; one inside the
    window is accepted once (rejected as a duplicate on a second sight); one older than the window is
    rejected as too-old. Not thread-safe — one window per sender link.
    """

    def __init__(
        self,
        window_size: int = 1024,
        *,
        initial_epoch: int | None = None,
        initial_highest: int = -1,
    ) -> None:
        if not isinstance(window_size, int) or window_size < 1:
            raise ValueError("window_size must be a positive int")
        self._size = window_size
        self._mask = (1 << window_size) - 1
        self._epoch: int | None = None
        self._highest = -1
        self._bitmap = 0  # bit i set == counter (highest - i) has been seen
        if initial_epoch is not None:
            # Restore persisted anti-replay state after a receiver restart. We can't know WHICH
            # in-window counters were already seen, so take the conservative posture: mark the whole
            # window as seen (bitmap = all ones) so ONLY a strictly-newer counter is accepted. This
            # prevents a captured old frame from replaying across a restart (closes the restart gap).
            if not isinstance(initial_epoch, int) or isinstance(initial_epoch, bool) or initial_epoch < 0:
                raise ValueError("initial_epoch must be a non-negative int")
            if not isinstance(initial_highest, int) or initial_highest < -1:
                raise ValueError("initial_highest must be an int >= -1")
            self._epoch = initial_epoch
            self._highest = initial_highest
            self._bitmap = self._mask if initial_highest >= 0 else 0

    @property
    def epoch(self) -> int | None:
        return self._epoch

    @property
    def highest(self) -> int:
        return self._highest

    def check_and_update(self, epoch: int, counter: int) -> bool:
        """Return True and record the frame if it is fresh; return False (record nothing) if it is a
        replay, an old epoch, or older than the window. Call this only for **authenticated** frames.
        """
        if (
            not isinstance(epoch, int)
            or not isinstance(counter, int)
            or isinstance(epoch, bool)
            or isinstance(counter, bool)
            or epoch < 0
            or counter < 0
        ):
            return False

        if self._epoch is None or epoch > self._epoch:
            # First frame ever, or a newer epoch -> start a fresh window at this counter.
            self._epoch = epoch
            self._highest = counter
            self._bitmap = 1  # bit 0 == highest == this counter, marked seen
            return True

        if epoch < self._epoch:
            return False  # stale epoch

        # Same epoch.
        if counter > self._highest:
            shift = counter - self._highest
            if shift >= self._size:
                # The whole old window rolls off — only the new head remains. Short-circuit so a huge
                # (authentic) counter jump can't force a multi-gigabyte `bitmap << shift` before the mask
                # truncates it (a resource-exhaustion hazard on a long-lived, low-epoch link).
                self._bitmap = 1
            else:
                # Shift the window up; bit 0 marks the new head. Bits past the window drop off.
                self._bitmap = ((self._bitmap << shift) | 1) & self._mask
            self._highest = counter
            return True

        offset = self._highest - counter
        if offset >= self._size:
            return False  # older than the window
        bit = 1 << offset
        if self._bitmap & bit:
            return False  # already seen -> duplicate
        self._bitmap |= bit
        return True


__all__ = [
    "VERSION",
    "KEY_LEN",
    "TAG_LEN",
    "HEADER_LEN",
    "OVERHEAD",
    "ESP_NOW_MTU",
    "Frame",
    "NodeCryptoError",
    "AuthenticationError",
    "ReplayError",
    "NonceExhaustedError",
    "max_plaintext",
    "seal",
    "unseal",
    "FrameSealer",
    "ReplayWindow",
]
