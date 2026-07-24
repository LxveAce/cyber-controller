"""Meshtastic StreamAPI backend — the live protobuf integration (comms rework, Wave 8).

Ties the three hardware-free pieces together into a real Meshtastic integration:

    raw serial bytes  ─►  StreamFramer.feed  ─►  meshtastic_proto.decode_fromradio  ─►  typed events
    typed send call   ─►  meshtastic_proto.encode_*  ─►  StreamFramer.frame  ─►  raw serial write

A locally-USB-connected node is inside its own trust boundary, so it delivers channel traffic to us
**already decrypted** — CC needs no channel crypto to read or send text on the node's own channels
(meshtastic.org/docs encryption boundary). This backend is transport-agnostic and Qt-free: it takes a
``writer`` callback (real use: ``SerialConnection.write_bytes``) and fans decoded state out through an
``on_event`` callback, so it is fully testable offline and was validated against a real Heltec V3 stream.

It holds the read/send SEMANTICS. The honest, empty text-CLI surface stays in ``meshtastic.py`` (no dead
buttons); this is the separate, real, structured path a stream device gets instead.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from src.protocols import meshtastic_proto as mp
from src.protocols.stream_framer import StreamFramer

log = logging.getLogger(__name__)

# Default want_config id. Any uint32 works — the node echoes it back as config_complete_id — but this exact
# value was accepted + answered by a real Heltec V3 during the Wave-8 HIL capture. Not NODELESS_WANT_CONFIG_ID
# (69420), so the node includes every neighbour's NodeInfo in the config dump.
_DEFAULT_CONFIG_ID = 0x12345678

_TEXT_BUF_CAP = 8192  # bound the debug-text accumulator so a newline-less flood can't grow it unbounded


class MeshtasticBackend:
    """Stateful StreamAPI client for one connected Meshtastic node.

    Usage (wired to a live raw-mode SerialConnection)::

        backend = MeshtasticBackend(conn.write_bytes, on_event=hub_adapter, on_text=log_sink)
        conn.raw = True
        conn.on_bytes(backend.feed_bytes)
        backend.start()                 # send want_config -> node streams its state back
        ...
        backend.send_text("hello", channel=0)
    """

    def __init__(
        self,
        writer: Callable[[bytes], None],
        on_event: Callable[[str, dict], None] | None = None,
        on_text: Callable[[str], None] | None = None,
        config_id: int = _DEFAULT_CONFIG_ID,
    ) -> None:
        self._writer = writer
        self._on_event = on_event
        self._on_text = on_text
        self.config_id = config_id if config_id != mp.NODELESS_WANT_CONFIG_ID else config_id + 1

        self._framer = StreamFramer(on_skipped=self._on_skipped_bytes)
        self._text_buf = bytearray()

        # Live view of the node's world (last-writer-wins per key). The dicts are mutated on the serial
        # reader thread (feed_bytes) and read on the GUI thread (node_list/active_channels via the panel),
        # so a lock guards them — an unguarded generator read while the reader inserts a key raises
        # "dictionary changed size during iteration" on the GUI thread (an uncaught crash).
        self._lock = threading.Lock()
        self.nodes: dict[int, mp.MeshNode] = {}
        self.channels: dict[int, mp.MeshChannel] = {}
        self.my_node_num: int | None = None
        self.config_complete = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Kick off the handshake: send one ToRadio{want_config_id}. The node then streams my_info,
        node_info*, channel*, config*, ending with config_complete_id == our id."""
        self.config_complete = False
        self._write_payload(mp.encode_want_config(self.config_id))

    def close(self) -> None:
        """Best-effort clean link close (ToRadio{disconnect}). Never raises."""
        try:
            self._write_payload(mp.encode_disconnect())
        except Exception:  # noqa: BLE001 — teardown must not raise
            log.debug("meshtastic: disconnect frame not sent", exc_info=True)

    # ── RX path ──────────────────────────────────────────────────────────────

    def feed_bytes(self, data: bytes) -> None:
        """Feed raw serial bytes (the ``SerialConnection.on_bytes`` sink). Extracts and decodes every
        complete FromRadio frame now available; interstitial debug text routes to ``on_text``."""
        try:
            for payload in self._framer.feed(data):
                self._handle_fromradio(payload)
        except Exception:  # noqa: BLE001 — a decode fault must not kill the reader thread
            log.exception("meshtastic: FromRadio handling error")

    def _handle_fromradio(self, payload: bytes) -> None:
        res = mp.decode_fromradio(payload)
        if res.kind == "my_info":
            self.my_node_num = res.my_node_num
            self._emit("mesh_my_info", {
                "num": res.my_node_num,
                "node_id": mp.node_id_str(res.my_node_num or 0),
            })
        elif res.kind == "node_info" and res.node is not None:
            n = res.node
            n.is_local = n.num == self.my_node_num
            with self._lock:
                self.nodes[n.num] = n
            self._emit("mesh_node", self._node_dict(n))
        elif res.kind == "channel" and res.channel is not None:
            c = res.channel
            if c.index >= 0:
                with self._lock:
                    self.channels[c.index] = c
                self._emit("mesh_channel", {
                    "index": c.index, "name": c.name, "role": c.role, "role_name": c.role_name,
                })
        elif res.kind == "text" and res.text is not None:
            t = res.text
            self._emit("mesh_text", {
                "from_num": t.from_num, "from_id": mp.node_id_str(t.from_num),
                "to_num": t.to_num, "channel": t.channel, "text": t.text,
                "rx_snr": t.rx_snr, "rx_rssi": t.rx_rssi, "packet_id": t.packet_id,
            })
        elif res.kind == "config_complete":
            self.config_complete = True
            self._emit("mesh_config_complete", {
                "config_id": res.config_complete_id,
                "node_count": len(self.nodes),
                "channel_count": len(self.channels),
            })

    def _on_skipped_bytes(self, data: bytes) -> None:
        """Accumulate non-frame debug/boot text and emit complete lines to ``on_text``."""
        if self._on_text is None:
            return
        self._text_buf.extend(data)
        while True:
            nl = self._text_buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._text_buf[:nl]).decode("utf-8", "replace").rstrip("\r")
            del self._text_buf[: nl + 1]
            if line.strip():
                try:
                    self._on_text(line)
                except Exception:  # noqa: BLE001
                    log.debug("meshtastic: on_text sink error", exc_info=True)
        if len(self._text_buf) > _TEXT_BUF_CAP:  # flush an over-long unterminated tail so memory stays bounded
            self._text_buf.clear()

    # ── TX path (typed control API — licensed-band comms, danger='') ─────────

    def send_text(self, text: str, channel: int = 0, dest: int = mp.BROADCAST_NUM) -> None:
        """Send a text message on *channel* (default the primary, index 0), broadcast unless *dest* is set."""
        self._write_payload(mp.encode_text_message(text, channel=channel, dest=dest))

    def request_config(self) -> None:
        """Re-request the full node/channel/config dump (same as :meth:`start`)."""
        self.start()

    def send_heartbeat(self) -> None:
        """Keep the serial link alive (ToRadio{heartbeat})."""
        self._write_payload(mp.encode_heartbeat())

    # ── views ────────────────────────────────────────────────────────────────

    def node_list(self) -> list[mp.MeshNode]:
        """All known nodes, local node first. Snapshots under the lock (the reader thread mutates)."""
        with self._lock:
            nodes = list(self.nodes.values())
        return sorted(nodes, key=lambda n: (not n.is_local, n.num))

    def primary_channel(self) -> mp.MeshChannel | None:
        """The PRIMARY (role==1) channel, if the node has reported it yet."""
        with self._lock:
            chans = list(self.channels.values())
        return next((c for c in chans if c.role == 1), None)

    def active_channels(self) -> list[mp.MeshChannel]:
        """Channels that are not DISABLED (role != 0), by index."""
        with self._lock:
            chans = list(self.channels.values())
        return sorted((c for c in chans if c.role != 0), key=lambda c: c.index)

    # ── internals ────────────────────────────────────────────────────────────

    def _write_payload(self, payload: bytes) -> None:
        self._writer(StreamFramer.frame(payload))

    def _emit(self, event_type: str, data: dict) -> None:
        if self._on_event is not None:
            try:
                self._on_event(event_type, data)
            except Exception:  # noqa: BLE001 — an event sink must not break decoding
                log.exception("meshtastic: on_event sink error (%s)", event_type)

    @staticmethod
    def _node_dict(n: mp.MeshNode) -> dict:
        return {
            "num": n.num, "node_id": n.node_id, "long_name": n.long_name, "short_name": n.short_name,
            "hw_model": n.hw_model, "hw_model_name": n.hw_model_name, "snr": n.snr,
            "battery": n.battery, "last_heard": n.last_heard, "is_local": n.is_local,
        }
