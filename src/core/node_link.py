r"""NodeLink — a wireless node presented as a drop-in :class:`SerialConnection` (W1.0).

This is the transport facade that makes the whole downstream stack — ``DeviceManager``,
``TargetIngestor``, ``AutoRouter``, ``Broadcast``, the network graph — drive a **wireless** CC node
exactly like a wired serial device, with zero changes to any of them. It duck-types the
``SerialConnection`` public surface (``port``/``is_connected``/``connect``/``disconnect``/``write``/
``write_bytes``/``on_line``/``remove_line_callback``/``on_state_change``/``on_error``) and, underneath,
seals every outbound command and verifies every inbound frame with :mod:`src.core.node_crypto`.

Transport model (MVP): NodeLink wraps a *gateway* connection — itself any SerialConnection-shaped
object, e.g. a real ``SerialConnection`` to a **USB gateway dongle** acting as a wireless serial
cable. Sealed frames are binary, so they ride the gateway's text line channel as **base64 lines**
(base64 is control-char-free, so it passes the gateway's own injection guard); one frame per line.

Crypto wiring (important):
  * The controller and its node share ONE 32-byte per-node key, but the nonce is only epoch‖counter
    (``node_id`` is authenticated, not in the nonce — see node_crypto). Using the same key for BOTH
    directions would collide nonces (host and node both start at counter 0 -> reuse -> GCM break).
    So NodeLink **derives two directional sub-keys** via HKDF-SHA256 — ``host->node`` and
    ``node->host`` — and seals with one, opens with the other, per ``role``. Disjoint keys -> disjoint
    nonce spaces -> no reuse.
  * **Restart safety:** a nonce must also never repeat across NodeLink *lifetimes* on the same key.
    So the sender epoch defaults to a **random u32** (not 0) when no state is restored, which makes an
    accidental collision astronomically unlikely; and for a hard guarantee the caller persists
    ``tx_epoch``/``tx_counter`` (and ``rx_epoch``/``rx_highest`` for cross-restart anti-replay) and
    passes them back via the ``epoch``/``counter``/``rx_epoch``/``rx_highest`` args. Host-side
    provisioning (next step) owns that persistent store — until then, treat a process restart as a new
    random epoch.
  * Outbound text goes through the SAME control-byte guard as ``SerialConnection.write`` (reject C0 /
    DEL) so a routed value such as a scanned SSID can't smuggle extra commands to the node.
  * Inbound frames that fail authentication OR replay are **dropped silently — never surfaced** as a
    line, so a forged/replayed frame can't inject fake scan results or poison the target pool.

Keys are provided by the caller (host-side provisioning, next beat); NodeLink generates/stores no
secret. Live RF is bench-gated — this module is pure host-side plumbing, tested over a mock gateway.
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
import re
from typing import Any, Callable

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from src.core.node_crypto import (
    ESP_NOW_MTU,
    AuthenticationError,
    FrameSealer,
    KEY_LEN,
    ReplayError,
    ReplayWindow,
    max_plaintext,
    unseal,
)
from src.core.serial_handler import ConnectionState

log = logging.getLogger(__name__)

# HKDF context labels — fixed, versioned; both endpoints must agree. Not secret.
_LABEL_HOST_TO_NODE = b"cc-node-link v1 host->node"
_LABEL_NODE_TO_HOST = b"cc-node-link v1 node->host"


def _derive_dir_key(key: bytes, label: bytes, node_id: int) -> bytes:
    """Derive a 32-byte directional key from the shared per-node key (HKDF-SHA256).

    ``node_id`` is folded into the HKDF ``info`` as defense-in-depth: two nodes accidentally
    provisioned with the same key still get distinct directional keys, so their nonce spaces can't
    collide even though ``node_id`` is not part of the AEAD nonce itself.
    """
    info = label + b"|node=" + int(node_id).to_bytes(2, "big")
    return HKDF(algorithm=hashes.SHA256(), length=KEY_LEN, salt=None, info=info).derive(bytes(key))


def _reject_control_chars(text: str) -> None:
    """The exact command-injection guard SerialConnection.write applies: no C0 (0x00-0x1F) or DEL."""
    bad = [ch for ch in text if ord(ch) < 0x20 or ord(ch) == 0x7F]
    if bad:
        raise ValueError(
            f"Refusing to send command with embedded control character(s) "
            f"{[hex(ord(c)) for c in bad]} — possible command injection"
        )


class NodeLink:
    """A wireless CC node as a :class:`SerialConnection` stand-in. One NodeLink per node link.

    Args:
        gateway: the underlying transport (SerialConnection-shaped): ``connect``/``disconnect``/
            ``write``/``is_connected``/``on_line``/``remove_line_callback`` (+ optional
            ``on_state_change``). In production a real SerialConnection to the USB gateway dongle.
        key: the shared 32-byte per-node key (caller-provisioned; never generated here).
        node_id: this node's id (0..65535).
        role: ``"host"`` (the controller side, default) or ``"node"`` (the node side, for loopback/
            testing) — selects which directional key seals vs. opens.
    """

    def __init__(
        self,
        gateway: Any,
        key: bytes,
        node_id: int,
        *,
        role: str = "host",
        port: str | None = None,
        window_size: int = 1024,
        line_ending: str = "\n",
        encoding: str = "utf-8",
        baud: int = 115200,
        epoch: int | None = None,
        counter: int = 0,
        rx_epoch: int | None = None,
        rx_highest: int = -1,
    ) -> None:
        if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_LEN:
            raise ValueError(f"key must be exactly {KEY_LEN} bytes")
        if role not in ("host", "node"):
            raise ValueError("role must be 'host' or 'node'")

        k_h2n = _derive_dir_key(key, _LABEL_HOST_TO_NODE, node_id)
        k_n2h = _derive_dir_key(key, _LABEL_NODE_TO_HOST, node_id)
        # Host seals host->node and opens node->host; the node is the mirror image.
        seal_key, open_key = (k_h2n, k_n2h) if role == "host" else (k_n2h, k_h2n)

        self._gateway = gateway
        self._role = role
        self.node_id = node_id
        self.port = port or f"node:{node_id}"
        self.line_ending = line_ending
        self.encoding = encoding
        self.baud = baud

        # Sender nonce base. If the caller did NOT restore a persisted epoch, start from a RANDOM u32
        # so two fresh NodeLinks on the same key never deterministically collide at (epoch 0, counter 0)
        # — that would be catastrophic AES-GCM nonce reuse. For a hard guarantee across restarts,
        # persist tx_epoch/tx_counter (see the class docstring) and pass them back via epoch/counter.
        tx_epoch = epoch if epoch is not None else int.from_bytes(os.urandom(4), "big")
        self._sealer = FrameSealer(seal_key, node_id, epoch=tx_epoch, counter=counter)  # outbound
        self._open_key = open_key                                                        # inbound verify key
        self._window = ReplayWindow(window_size, initial_epoch=rx_epoch, initial_highest=rx_highest)
        self._max_pt = max_plaintext(ESP_NOW_MTU)

        self._line_callbacks: list[Callable[[str], None]] = []
        self._state_callbacks: list[Callable[[ConnectionState], None]] = []
        self._error_callbacks: list[Callable[[Exception], None]] = []
        self._rx_advance_cb: Callable[[], None] | None = None

        # Wire onto the gateway ONCE for our lifetime: inbound frames -> our decode path, and the
        # gateway's state transitions -> our subscribers. Both survive connect/disconnect cycles.
        self._gateway.on_line(self._on_gateway_line)
        gw_state = getattr(self._gateway, "on_state_change", None)
        if callable(gw_state):
            gw_state(self._emit_state)

    # ── Properties (mirror the gateway) ──────────────────────────────
    @property
    def state(self) -> ConnectionState:
        return getattr(self._gateway, "state", ConnectionState.DISCONNECTED)

    @property
    def is_connected(self) -> bool:
        return bool(getattr(self._gateway, "is_connected", False))

    # ── Persistence state (save these before teardown; pass them back on re-create) ──
    @property
    def tx_epoch(self) -> int:
        """Next outbound epoch — persist with :attr:`tx_counter` to avoid nonce reuse on restart."""
        return self._sealer.epoch

    @property
    def tx_counter(self) -> int:
        """Next outbound counter — persist with :attr:`tx_epoch`."""
        return self._sealer.counter

    @property
    def rx_epoch(self) -> int | None:
        """Highest inbound epoch seen — persist with :attr:`rx_highest` for cross-restart anti-replay."""
        return self._window.epoch

    @property
    def rx_highest(self) -> int:
        """Highest inbound counter accepted in :attr:`rx_epoch`."""
        return self._window.highest

    # ── Callback registration (same surface as SerialConnection) ─────
    def on_line(self, cb: Callable[[str], None]) -> None:
        self._line_callbacks.append(cb)

    def remove_line_callback(self, cb: Callable[[str], None]) -> None:
        try:
            self._line_callbacks.remove(cb)
        except ValueError:
            pass

    def on_state_change(self, cb: Callable[[ConnectionState], None]) -> None:
        self._state_callbacks.append(cb)

    def remove_state_callback(self, cb: Callable[[ConnectionState], None]) -> None:
        try:
            self._state_callbacks.remove(cb)
        except ValueError:
            pass

    def on_error(self, cb: Callable[[Exception], None]) -> None:
        self._error_callbacks.append(cb)

    def on_rx_advance(self, cb: Callable[[], None] | None) -> None:
        """Register a callback fired whenever an inbound frame is ACCEPTED (the anti-replay window head
        advanced). Lets the owner persist rx_epoch/rx_highest as the session runs so a crash can't roll the
        head back and re-open captured frames to replay. Keep it cheap + non-raising — it runs on the RX
        path; throttle any actual persistence inside it."""
        self._rx_advance_cb = cb

    def _detach_from_gateway(self) -> None:
        """Unhook OUR line + state callbacks from the (borrowed) gateway. Fully symmetric so a closed
        NodeLink leaves no dead callback behind on a gateway that outlives it (which leaked the NodeLink +
        its key material and fanned state events out to stale links across dongle reuse)."""
        for method, cb in (("remove_line_callback", self._on_gateway_line),
                           ("remove_state_callback", self._emit_state)):
            remover = getattr(self._gateway, method, None)
            if callable(remover):
                try:
                    remover(cb)
                except Exception:
                    pass

    # ── Lifecycle ────────────────────────────────────────────────────
    def connect(self) -> None:
        self._gateway.connect()

    def disconnect(self) -> None:
        # BORROWED gateway: unhook ourselves but do NOT tear down the shared physical port. One dongle
        # may gateway several nodes (and the Devices tab), so force-closing it here silently killed every
        # other consumer. The DeviceManager owner refcount decides when the gateway actually closes.
        self._detach_from_gateway()

    def close(self) -> None:
        """Detach this NodeLink from the (borrowed) gateway so it stops receiving/decoding under a
        now-stale key. Does NOT disconnect the shared gateway — its owner (via DeviceManager's refcount)
        does that when the last owner releases it."""
        self._detach_from_gateway()

    # ── Outbound I/O ─────────────────────────────────────────────────
    def write(self, data: str) -> None:
        """Send one command line to the node (control-byte guarded, sealed, base64-framed)."""
        cleaned = data.rstrip("\r\n")
        _reject_control_chars(cleaned)
        self._send_sealed((cleaned + self.line_ending).encode(self.encoding))

    def write_bytes(self, payload: bytes) -> None:
        """Send a raw byte payload to the node, sealed (no line terminator, no text guard)."""
        self._send_sealed(bytes(payload))

    def send_interrupt(self) -> None:
        """Relay a single Ctrl-C (0x03) to the node's firmware shell, sealed like any frame."""
        self._send_sealed(b"\x03")

    def _send_sealed(self, payload: bytes) -> None:
        if len(payload) > self._max_pt:
            # No fragmentation yet (W1.x) — refuse rather than silently truncate a command.
            raise ValueError(
                f"payload {len(payload)}B exceeds the node MTU budget ({self._max_pt}B); "
                f"fragmentation is not implemented yet"
            )
        frame = self._sealer.seal(payload)
        line = base64.b64encode(frame).decode("ascii")  # control-char-free -> passes the gateway guard
        self._gateway.write(line)

    # ── Inbound I/O ──────────────────────────────────────────────────
    def _on_gateway_line(self, line: str) -> None:
        """A base64 frame line arrived from the gateway: decode -> verify -> replay-check -> surface.

        Any failure (non-base64, forged/tampered, replayed, malformed) drops the line silently — it is
        never surfaced to on_line subscribers, so bad frames cannot inject commands or fake targets.
        """
        text = (line or "").strip()
        if not text:
            return
        try:
            frame = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            log.debug("NodeLink %s: dropped non-base64 line", self.port)
            return
        try:
            _, plaintext = unseal(self._open_key, frame, replay=self._window)
        except AuthenticationError:
            log.debug("NodeLink %s: dropped unauthenticated (forged/tampered) frame", self.port)
            return
        except ReplayError:
            log.debug("NodeLink %s: dropped replayed/stale frame", self.port)
            return
        except Exception:  # noqa: BLE001 — any malformed frame is dropped, never surfaced/raised
            log.debug("NodeLink %s: dropped malformed frame", self.port)
            return

        # Frame accepted -> the replay-window head just advanced. Signal the owner so it can persist the
        # new head (throttled); a crash then rolls anti-replay back by at most a bounded window rather than
        # to the last clean detach (which would let every frame captured since then replay).
        cb = self._rx_advance_cb
        if cb is not None:
            try:
                cb()
            except Exception:  # noqa: BLE001 — persistence must never break the RX path
                log.exception("NodeLink %s: rx-advance callback error", self.port)

        decoded = plaintext.decode(self.encoding, errors="replace")
        # The node relays the firmware's own line(s); frame on any terminator, emit each non-empty line.
        for part in re.split(r"[\r\n]+", decoded):
            if part:
                self._emit_line(part)

    # ── Internal fan-out (mirrors SerialConnection) ──────────────────
    def _emit_line(self, line: str) -> None:
        for cb in list(self._line_callbacks):
            try:
                cb(line)
            except Exception:
                log.exception("NodeLink line callback error")

    def _emit_state(self, new_state: ConnectionState) -> None:
        for cb in list(self._state_callbacks):
            try:
                cb(new_state)
            except Exception:
                log.exception("NodeLink state callback error")

    def _emit_error(self, exc: Exception) -> None:
        for cb in list(self._error_callbacks):
            try:
                cb(exc)
            except Exception:
                log.exception("NodeLink error callback error")

    # ── Context manager (parity with SerialConnection) ───────────────
    def __enter__(self) -> "NodeLink":
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()


__all__ = ["NodeLink"]
