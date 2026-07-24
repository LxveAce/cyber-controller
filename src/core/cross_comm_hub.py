"""Cross-comm hub — the single assembly point for the device-node spine.

Before this, the six cross-comm parts (EventBus, TargetPool, TargetIngestor, AutoRouter, BroadcastEngine,
ActionResolver) plus the "write a routed command to a port" callback were hand-wired inside the Qt main
window's ``__init__`` — six independently-constructed peers tangled up with UI setup. That is exactly the
"clunky cross-comm" the rework targets.

:class:`CrossCommHub` collapses that assembly into one place and one object. Given a
:class:`DeviceManager` (the node registry), it builds and owns the whole cross-comm layer and exposes each
part as an attribute, so a UI is a *thin consumer* (``hub.router``, ``hub.pool``, …) instead of the assembler.
The routed-command sink (:meth:`send_to_port`) lives here too — it is pure core logic (resolve the target's
line terminator, write to its serial connection), with no Qt dependency, so it belongs on the spine rather
than on a window.

This is behavior-preserving: same parts, same wiring, just assembled once in core. See the internal
cross-comm rework notes (stage S2).
"""

from __future__ import annotations

import logging

from src.core.capture_correlate import CaptureCorrelator
from src.core.capture_store import CaptureStore
from src.core.cross_comm import AutoRouter, EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.drivers import driver_for
from src.core.target_ingest import TargetIngestor

log = logging.getLogger(__name__)


class CrossCommHub:
    """The device-node spine: one owner for the whole cross-comm layer.

    Attributes:
        dm: The DeviceManager node registry (what's connected, what it can do).
        bus: The EventBus every node/target/action change publishes to.
        pool: The shared TargetPool (discovery view over the bus).
        captures: The shared CaptureStore (captured WPA handshakes / PMKIDs, over the bus).
        correlator: CaptureCorrelator — ties a fired deauth to the handshake it produces.
        ingestor: Feeds each device's parsed serial output into the pool (and the capture log).
        router: AutoRouter — routing rules as event subscribers; fires via :meth:`send_to_port`.
        broadcast: BroadcastEngine — one verb fans out to every capable node in its native command.
        action_resolver: Maps a target to firmware-specific actions per node. May be ``None`` if the
            optional action-resolver layer is unavailable (import/creation failure) — callers already guard.
    """

    def __init__(
        self,
        device_manager: DeviceManager,
        bus: EventBus | None = None,
        pool: TargetPool | None = None,
    ) -> None:
        self.dm = device_manager
        self.bus = bus or EventBus()
        self.pool = pool if pool is not None else TargetPool(self.bus)

        # The shared capture log — captured WPA handshakes / PMKIDs, keyed like the pool and on
        # the same bus (capture.* mirroring target.*). The ingestor auto-registers a capture
        # whenever a device reports one; the Crack Lab's Captures list rides capture.added live.
        self.captures = CaptureStore(self.bus)

        # Capture-confirm correlator (punch-list #2, slice 5): a bus-only observer that ties a fired
        # deauth (action.executed with a target BSSID + chain_events) to the handshake it produces
        # (a capture.added for that BSSID inside a window) -> capture.confirmed. No radio/commands.
        # A Qt timer in the window drives correlator.sweep() to surface honest timeouts.
        self.correlator = CaptureCorrelator(self.bus)

        # Feeds parsed APs/clients from each connected device into the shared pool, completing the loop:
        # a scan on device A -> target.added -> AutoRouter -> a command on device B. The hub owns the one
        # instance everyone shares AND auto-attaches it to every connection the moment it opens (below).
        # It also feeds captured handshakes/PMKIDs into the shared capture log.
        self.ingestor = TargetIngestor(self.pool, captures=self.captures, devices=self.dm)

        # Per-port Meshtastic StreamAPI backends. A stream device has no text line channel, so instead of
        # the line ingestor it gets a MeshtasticBackend on the raw byte path (protobuf decode + typed send).
        # Decoded node/channel/text state fans onto the bus under ``mesh.*`` topics; the UI reads it via
        # :meth:`mesh_backend`. Keyed by port; the parallel ``_mesh_conns`` map makes re-attach idempotent.
        self.mesh_backends: dict = {}
        self._mesh_conns: dict = {}

        # Attach the ingestor to EVERY connection the DeviceManager opens — Devices-tab Connect, Wardrive,
        # Broadcast, or an injected NodeLink — so a scan on ANY opened device feeds the pool, not only a
        # Devices-tab Connect (which was the sole attach site before, leaving the Targets tab empty during
        # wardriving/broadcasting). Re-attach is idempotent (TargetIngestor dedups per port).
        self.dm.on_connection_opened(self._attach_ingestor)

        # Track firmware changes too: a device's firmware is often set AFTER open_connection fires the attach
        # (the Devices tab persists it post-connect, or auto-detect resolves it later), so gating the stream
        # backend only on open-time firmware would leave a Meshtastic panel inert on first Connect. Attach the
        # backend the moment the firmware resolves to a stream device (and detach if it changes away).
        self.dm.on_device_changed(self._on_device_changed)

        # Routing rules engine — subscribes to target.added and dispatches via our own send sink.
        self.router = AutoRouter(self.bus, self.send_to_port)

        # Unified Action Broadcast — one verb -> every connected device's native command; results
        # converge via the same ingestor/pool. Imported locally to match the window's original layering.
        from src.core.broadcast import BroadcastEngine
        self.broadcast = BroadcastEngine(self.dm, self.bus)

        # Action resolver maps targets to firmware-specific actions per node. Optional: degrade gracefully
        # to None (actions disabled) exactly as the window did, rather than failing app start.
        self.action_resolver = None
        try:
            from src.core.action_resolver import ActionResolver
            self.action_resolver = ActionResolver(self.dm)
            log.info("ActionResolver initialized")
        except Exception:  # noqa: BLE001 — optional layer; app runs without it
            log.warning("ActionResolver unavailable — actions disabled", exc_info=True)

    def _attach_ingestor(self, port: str, conn) -> None:
        """Attach the shared TargetIngestor to a newly-opened *conn*, parsing with the device's own
        firmware protocol (default 'marauder', matching the Devices tab) so its scans feed the pool."""
        from src.protocols import get_protocol

        dev = self.dm.get_device(port)
        fw = (getattr(dev, "firmware", "") if dev else "") or "marauder"
        try:
            self.ingestor.attach(conn, get_protocol(fw))
        except Exception:
            log.exception("cross-comm: ingestor auto-attach failed for %s", port)

        # A stream device (Meshtastic protobuf StreamAPI) has no text line channel — the line ingestor above
        # will never see a line from it. Attach a structured backend on the raw byte path instead so its
        # nodes/channels/text decode and its typed send API is live. driver_type_for() returns "stream" only
        # for Meshtastic today, so no text-CLI device is disturbed.
        try:
            from src.protocols import driver_type_for

            if driver_type_for(fw) == "stream":
                self._attach_stream_backend(port, conn)
        except Exception:
            log.exception("cross-comm: stream backend attach failed for %s", port)

    def _attach_stream_backend(self, port: str, conn) -> None:
        """Attach a :class:`MeshtasticBackend` to a stream device's raw byte path (idempotent per conn).

        Flips the connection into raw mode, wires the protobuf decoder to ``on_bytes``, fans decoded state
        onto the bus under ``mesh.*`` topics + debug lines under ``mesh.log``, and kicks off the want_config
        handshake so the node streams its nodes/channels/config. Re-attaching to the SAME live connection is
        a no-op (guarded by ``_mesh_conns``); a fresh reconnect builds a new backend."""
        if self._mesh_conns.get(port) is conn:
            return  # already wired to this exact connection
        from src.protocols.meshtastic_stream import MeshtasticBackend

        def _on_event(event_type: str, data: dict, _port: str = port) -> None:
            # "mesh_node" -> bus topic "mesh.node"; carry the source port so a multi-node UI can key on it.
            topic = "mesh." + (event_type[5:] if event_type.startswith("mesh_") else event_type)
            self.bus.publish(topic, {"port": _port, **data})

        def _on_text(line: str, _port: str = port) -> None:
            self.bus.publish("mesh.log", {"port": _port, "line": line})

        conn.raw = True
        backend = MeshtasticBackend(conn.write_bytes, on_event=_on_event, on_text=_on_text)
        conn.on_bytes(backend.feed_bytes)
        self.mesh_backends[port] = backend
        self._mesh_conns[port] = conn
        # Expose the backend on the connection so a UI holding only the connection (the Devices-tab
        # Meshtastic panel) can drive send_text without a hub reference.
        conn.mesh_backend = backend

        # Clean up when this connection drops (there's no on_connection_closed hook) so the backend + the
        # dict entries + conn.mesh_backend don't linger stale after a disconnect/unplug.
        def _on_conn_state(state, _port=port, _conn=conn):
            from src.core.serial_handler import ConnectionState
            if state in (ConnectionState.DISCONNECTED, ConnectionState.ERROR):
                if self._mesh_conns.get(_port) is _conn:
                    self._detach_stream_backend(_port)

        conn.on_state_change(_on_conn_state)
        backend.start()  # want_config — a read request; safe/non-destructive
        log.info("cross-comm: Meshtastic stream backend attached on %s", port)

    def _detach_stream_backend(self, port: str) -> None:
        """Drop the stream backend for *port*: remove it from tracking, unhook its byte callback, restore the
        connection to line mode, and clear ``conn.mesh_backend``. Safe if none is attached."""
        backend = self.mesh_backends.pop(port, None)
        conn = self._mesh_conns.pop(port, None)
        if conn is not None:
            conn.raw = False  # restore text-line mode for the next (text-CLI) firmware on this port
            if backend is not None:
                try:
                    conn.remove_byte_callback(backend.feed_bytes)
                except Exception:  # noqa: BLE001
                    pass
            if getattr(conn, "mesh_backend", None) is backend:
                conn.mesh_backend = None

    def _on_device_changed(self, dev) -> None:
        """A device's firmware changed: attach the stream backend if it is now a stream device with a live
        connection, or detach it if it changed away from one. Covers the first-Connect timing (firmware set
        after open) and a mid-session firmware switch / auto-detect result."""
        port = getattr(dev, "port", "") or ""
        if not port:
            return
        conn = self.dm.get_connection(port)
        if conn is None or not getattr(conn, "is_connected", False):
            return  # a backend only matters for a live connection
        from src.protocols import driver_type_for

        fw = getattr(dev, "firmware", "") or ""
        try:
            if driver_type_for(fw) == "stream":
                self._attach_stream_backend(port, conn)  # idempotent per conn
            elif self._mesh_conns.get(port) is conn:
                self._detach_stream_backend(port)  # firmware changed off a stream device
        except Exception:
            log.exception("cross-comm: stream backend (re)attach on device-change failed for %s", port)

    def mesh_backend(self, port: str):
        """The :class:`MeshtasticBackend` for a connected stream device on *port*, or ``None``. The UI's
        Meshtastic panel reads node/channel state and drives ``send_text`` through this."""
        return self.mesh_backends.get(port)

    def send_to_port(self, port: str, command: str) -> None:
        """Deliver a command to a connected device (the AutoRouter / Network-tab send sink).

        Pure core. The *how* is delegated to the node's :class:`~src.core.drivers.Driver` (selected by its
        ``driver_type``): a text-CLI node gets the firmware terminator stamped + a serial write; a stream
        (Meshtastic protobuf) or control-map (BlueJammer web-UI) node has no text command channel, so the
        command is an honest logged no-op rather than useless bytes on the wire. No-ops with a warning when
        the port has no live connection.
        """
        conn = self.dm.get_connection(port)
        if not (conn and conn.is_connected):
            log.warning("send_to_port: no active connection on %s for routed command", port)
            return
        dev = self.dm.get_device(port)
        try:
            driver_for(dev).deliver_text(conn, dev, command)
            # A device list-clear/reboot through THIS sink flushes the port's parser scan
            # ordinals so a later `select -a {index}` binds right. The reset lives on the
            # ingestor (which owns the per-port parser) so the Devices-tab terminal Send
            # shares the exact same path — see TargetIngestor.note_command_sent. NOT fired
            # on a UI `target.cleared` pool wipe (the on-device list stays populated there).
            self.ingestor.note_command_sent(port, command)
        except Exception:
            log.exception("send_to_port %s failed", port)
