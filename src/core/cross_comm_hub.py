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
import threading
import time

from src.core.capture_correlate import CaptureCorrelator
from src.core.capture_store import CaptureStore
from src.core.cross_comm import AutoRouter, EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.drivers import driver_for
from src.core.lifecycle import CallbackScope
from src.core.sensing_model import SensingModel
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
        captures_persist_path: str | None = None,
        *,
        defer: bool = False,
    ) -> None:
        self.dm = device_manager
        self.bus = bus or EventBus()
        self.pool = pool if pool is not None else TargetPool(self.bus)
        self._callbacks = CallbackScope()
        self.correlator = self.ingestor = self.router = None
        self.mesh_backends: dict = {}
        self._mesh_conns: dict = {}
        self._mesh_devices: dict = {}
        self._mesh_callbacks: dict = {}
        self._mesh_attach_lock = threading.RLock()
        self._mesh_state_lock = threading.Lock()
        self._captures_persist_path = captures_persist_path
        self._init_lock = threading.Lock()
        self._initialized = False
        # Eager (default): init now with no sink, preserving bare/test/legacy behavior.
        # Deferred: keep the hub inert until initialize() injects a started sink.
        if not defer:
            try:
                self._initialize(captures_persist_path)   # eager: original call, no history sink
                self._initialized = True
            except BaseException as original:
                try:
                    self.close()
                except BaseException as cleanup_error:
                    raise original from cleanup_error
                raise

    def initialize(self, *, journal=None) -> None:
        """One-shot deferred init with an optional ALREADY-STARTED history sink. Reserves the
        attempt under a short state lock (rejecting a repeat or a fenced/closed hub), then runs the
        subscription-bearing init inside a CallbackScope activity lease so a concurrent close
        fences and waits. On failure the exception propagates and the lease is released before the
        owner's failure-driven close; a partially failed initialize is not retried."""
        with self._init_lock:
            if self._initialized:
                raise RuntimeError("CrossCommHub.initialize() is one-shot")
            if self._callbacks.closed:
                raise RuntimeError("cannot initialize a fenced/closed hub")
            self._initialized = True   # reserve before unlocking; never held across callbacks
        with self._callbacks.activity():
            self._initialize(self._captures_persist_path, journal=journal)

    def _initialize(self, captures_persist_path: str | None, *, journal=None) -> None:

        # The shared capture log — captured WPA handshakes / PMKIDs, keyed like the pool and on
        # the same bus (capture.* mirroring target.*). The ingestor auto-registers a capture
        # whenever a device reports one; the Crack Lab's Captures list rides capture.added live.
        self.captures = CaptureStore(self.bus, persist_path=captures_persist_path)

        # Capture-confirm correlator (punch-list #2, slice 5): a bus-only observer that ties a fired
        # deauth (action.executed with a target BSSID + chain_events) to the handshake it produces
        # (a capture.added for that BSSID inside a window) -> capture.confirmed. No radio/commands.
        # A Qt timer in the window drives correlator.sweep() to surface honest timeouts.
        self.correlator = CaptureCorrelator(self.bus)

        # Feeds parsed APs/clients from each connected device into the shared pool, completing the loop:
        # a scan on device A -> target.added -> AutoRouter -> a command on device B. The hub owns the one
        # instance everyone shares AND auto-attaches it to every connection the moment it opens (below).
        # It also feeds captured handshakes/PMKIDs into the shared capture log.
        self.ingestor = TargetIngestor(
            self.pool, captures=self.captures, devices=self.dm, journal=journal)

        # Wi-Fi CSI sensing rollup: a connected sensing node's `sensing_verdict` events (parsed
        # by csi_sensor) fold into per-node room state here — RX-only awareness, NEVER a Target row.
        # The observer taps the full parsed-event stream (the seam BLE Analyzer uses) + a monotonic
        # clock; a "Sense" view reads this model. No effect for any non-sensing firmware.
        self.sensing = SensingModel()
        self.ingestor.add_event_observer(self._on_parsed_event)

        # Per-port Meshtastic StreamAPI backends. A stream device has no text line channel, so instead of
        # the line ingestor it gets a MeshtasticBackend on the raw byte path (protobuf decode + typed send).
        # Decoded node/channel/text state fans onto the bus under ``mesh.*`` topics; the UI reads it via
        # :meth:`mesh_backend`. Keyed by port; the parallel ``_mesh_conns`` map makes re-attach idempotent.
        # Attach the ingestor to EVERY connection the DeviceManager opens — Devices-tab Connect, Wardrive,
        # Broadcast, or an injected NodeLink — so a scan on ANY opened device feeds the pool, not only a
        # Devices-tab Connect (which was the sole attach site before, leaving the Targets tab empty during
        # wardriving/broadcasting). Re-attach is idempotent (TargetIngestor dedups per port).
        self._callbacks.register(
            self.dm.on_connection_opened, self.dm.remove_connection_opened_callback,
            self._attach_ingestor,
        )

        # Track firmware changes too: a device's firmware is often set AFTER open_connection fires the attach
        # (the Devices tab persists it post-connect, or auto-detect resolves it later), so gating the stream
        # backend only on open-time firmware would leave a Meshtastic panel inert on first Connect. Attach the
        # backend the moment the firmware resolves to a stream device (and detach if it changes away).
        self._callbacks.register(
            self.dm.on_device_changed, self.dm.remove_device_changed_callback,
            self._on_device_changed,
        )

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

    def fence(self) -> None:
        """Stop admitting new callbacks before runtime teardown waits for committed work."""
        self._callbacks.fence()
        for service in (self.ingestor, self.router, self.correlator):
            if service is not None:
                service.fence()

    def close(self, timeout: float | None = 5.0) -> None:
        """Remove this hub's subscriptions without closing shared devices or connections."""
        self.fence()
        self._callbacks.close(timeout)
        for service in (self.ingestor, self.router, self.correlator):
            if service is not None:
                service.close(timeout)
        for port in tuple(self._mesh_conns):
            self._detach_stream_backend(port)

    def _on_parsed_event(self, ev, port: str) -> None:
        """Fold a CSI ``sensing_verdict`` into the sensing model (RX-only). Fires on the serial
        reader thread for every parsed event; a cheap event-type gate keeps other firmware clear."""
        if getattr(ev, "event_type", "") == "sensing_verdict":
            self.sensing.observe(getattr(ev, "data", None), time.monotonic())

    def _attach_ingestor(self, port: str, conn) -> None:
        """Attach the shared TargetIngestor to a newly-opened *conn*, parsing with the device's own
        firmware protocol (default 'marauder', matching the Devices tab) so its scans feed the pool."""
        from src.protocols import get_protocol, resolve_protocol_name

        dev = self.dm.get_device(port)
        fw = (getattr(dev, "firmware", "") if dev else "") or "marauder"
        canonical = resolve_protocol_name(fw)

        def is_current() -> bool:
            return (self.dm.get_device(port) is dev and self.dm.get_connection(port) is conn
                    and getattr(conn, "is_connected", False)
                    and resolve_protocol_name(getattr(dev, "firmware", "") or "marauder")
                    == canonical)

        try:
            if not is_current():
                return
            if self.ingestor.reconcile(conn, get_protocol(canonical or fw), is_current=is_current) is None:
                return
        except Exception:
            log.exception("cross-comm: ingestor auto-attach failed for %s", port)
            return

        # A stream device (Meshtastic protobuf StreamAPI) has no text line channel — the line ingestor above
        # will never see a line from it. Attach a structured backend on the raw byte path instead so its
        # nodes/channels/text decode and its typed send API is live. driver_type_for() returns "stream" only
        # for Meshtastic today, so no text-CLI device is disturbed.
        try:
            from src.protocols import driver_type_for

            if not is_current():
                return
            if driver_type_for(fw) == "stream":
                self._attach_stream_backend(port, conn)
            elif self._mesh_conns.get(port) is conn:
                self._detach_stream_backend(port, expected_connection=conn)
        except Exception:
            log.exception("cross-comm: stream backend attach failed for %s", port)

    def _attach_stream_backend(self, port: str, conn) -> None:
        """Attach a :class:`MeshtasticBackend` to a stream device's raw byte path (idempotent per conn).

        Flips the connection into raw mode, wires the protobuf decoder to ``on_bytes``, fans decoded state
        onto the bus under ``mesh.*`` topics + debug lines under ``mesh.log``, and kicks off the want_config
        handshake so the node streams its nodes/channels/config. Re-attaching to the SAME live connection is
        a no-op (guarded by ``_mesh_conns``); a fresh reconnect builds a new backend."""
        with self._mesh_attach_lock:
            self._replace_stream_backend(port, conn)

    def _replace_stream_backend(self, port: str, conn) -> None:
        if (self._mesh_conns.get(port) is conn
                and self._mesh_devices.get(port) is self.dm.get_device(port)):
            return  # already wired to this exact connection
        if port in self._mesh_conns:
            self._detach_stream_backend(port)
        try:
            self._install_stream_backend(port, conn)
        except BaseException:
            self._detach_stream_backend(port, expected_connection=conn)
            raise

    def _install_stream_backend(self, port: str, conn) -> None:
        from src.protocols import resolve_protocol_name
        from src.protocols.meshtastic_stream import MeshtasticBackend

        device = self.dm.get_device(port)

        def _is_current() -> bool:
            if self._callbacks.closed:
                return False
            with self._mesh_state_lock:
                current = (self._mesh_conns.get(port) is conn
                           and self.mesh_backends.get(port) is backend)
            return (current and self.dm.get_device(port) is device
                    and self.dm.get_connection(port) is conn
                    and getattr(conn, "mesh_backend", None) is backend
                    and getattr(conn, "is_connected", False)
                    and resolve_protocol_name(getattr(device, "firmware", "")) == "meshtastic")

        def _on_event(event_type: str, data: dict, _port: str = port) -> None:
            if not _is_current():
                return
            # A source may retire after this check. Never relabel its event with a replacement's ID;
            # consumers must match the session token. Publish outside locks that subscribers reenter.
            topic = "mesh." + (event_type[5:] if event_type.startswith("mesh_") else event_type)
            self.bus.publish(topic, {**data, "port": _port, "session_id": backend.session_id})

        def _on_text(line: str, _port: str = port) -> None:
            if _is_current():
                self.bus.publish("mesh.log", {"port": _port, "session_id": backend.session_id, "line": line})

        backend = MeshtasticBackend(conn.write_bytes, on_event=_on_event, on_text=_on_text)

        def _feed_current(data: bytes) -> None:
            if _is_current():
                backend.feed_bytes(data)

        byte_callback = self._callbacks.guard(_feed_current)
        conn.raw = True
        with self._mesh_state_lock:
            self.mesh_backends[port] = backend
            self._mesh_conns[port] = conn
            self._mesh_devices[port] = device
            self._mesh_callbacks[port] = (byte_callback, None)
        # Expose the backend on the connection so a UI holding only the connection (the Devices-tab
        # Meshtastic panel) can drive send_text without a hub reference.
        conn.mesh_backend = backend
        conn.on_bytes(byte_callback)

        # Clean up when this connection drops (there's no on_connection_closed hook) so the backend + the
        # dict entries + conn.mesh_backend don't linger stale after a disconnect/unplug.
        def _on_conn_state(state, _port=port, _conn=conn, _backend=backend):
            from src.core.serial_handler import ConnectionState
            if state in (ConnectionState.DISCONNECTED, ConnectionState.ERROR):
                if self._mesh_conns.get(_port) is _conn:
                    self._detach_stream_backend(
                        _port, expected_connection=_conn, expected_backend=_backend,
                    )

        state_callback = self._callbacks.guard(_on_conn_state)
        with self._mesh_state_lock:
            self._mesh_callbacks[port] = (byte_callback, state_callback)
        conn.on_state_change(state_callback)
        backend.start()  # want_config — a read request; safe/non-destructive
        log.info("cross-comm: Meshtastic stream backend attached on %s", port)

    def _detach_stream_backend(
        self, port: str, *, expected_connection=None, expected_backend=None,
    ) -> None:
        """Drop the stream backend for *port*: remove it from tracking, unhook its byte callback, restore the
        connection to line mode, and clear ``conn.mesh_backend``. Safe if none is attached."""
        with self._mesh_state_lock:
            backend = self.mesh_backends.get(port)
            conn = self._mesh_conns.get(port)
            callbacks = self._mesh_callbacks.get(port)
            if expected_connection is not None and conn is not expected_connection:
                return
            if expected_backend is not None and backend is not expected_backend:
                return
        if conn is not None:
            if backend is not None:
                retire = getattr(backend, "retire", None)
                if retire is not None:
                    retire()  # local data fence only; shared serial ownership and writes are unchanged
                conn.remove_byte_callback(callbacks[0] if callbacks else backend.feed_bytes)
                if callbacks and callbacks[1] is not None:
                    conn.remove_state_callback(callbacks[1])
            if getattr(conn, "mesh_backend", None) is backend:
                conn.raw = False  # only restore a stream still owned by this hub
                conn.mesh_backend = None
        with self._mesh_state_lock:
            if (self._mesh_conns.get(port) is conn and self.mesh_backends.get(port) is backend
                    and self._mesh_callbacks.get(port) is callbacks):
                self.mesh_backends.pop(port, None)
                self._mesh_conns.pop(port, None)
                self._mesh_devices.pop(port, None)
                self._mesh_callbacks.pop(port, None)

    def _on_device_changed(self, dev) -> None:
        """Reconcile the current text parser or stream backend after firmware identification."""
        port = getattr(dev, "port", "") or ""
        if not port or self.dm.get_device(port) is not dev:
            return
        conn = self.dm.get_connection(port)
        if conn is None or not getattr(conn, "is_connected", False):
            return  # a backend only matters for a live connection
        # Same-protocol notifications preserve the parser and its scan ordinals.
        self._attach_ingestor(port, conn)

    def mesh_backend(self, port: str):
        """The :class:`MeshtasticBackend` for a connected stream device on *port*, or ``None``. The UI's
        Meshtastic panel reads node/channel state and drives ``send_text`` through this."""
        return self.mesh_backends.get(port)

    def send_to_port(self, port: str, command: str) -> bool:
        """Deliver a command to a connected device (the AutoRouter / Network-tab send sink).

        Pure core. The *how* is delegated to the node's :class:`~src.core.drivers.Driver` (selected by its
        ``driver_type``): a text-CLI node gets the firmware terminator stamped + a serial write; a stream
        (Meshtastic protobuf) or control-map (BlueJammer web-UI) node has no text command channel, so the
        command is an honest logged refusal rather than useless bytes on the wire. Returns whether the
        driver accepted the delivery. Refuses with a warning when the port has no live connection.
        """
        conn = self.dm.get_connection(port)
        if not (conn and conn.is_connected):
            log.warning("send_to_port: no active connection on %s for routed command", port)
            return False
        dev = self.dm.get_device(port)
        try:
            delivered = driver_for(dev).deliver_text(conn, dev, command)
        except Exception:
            log.exception("send_to_port %s failed", port)
            return False
        if not delivered:
            log.warning("send_to_port: driver refused text delivery on %s", port)
            return False
        # A device list-clear/reboot through THIS sink flushes the port's parser scan
        # ordinals so a later `select -a {index}` binds right. The reset lives on the
        # ingestor (which owns the per-port parser) so the Devices-tab terminal Send
        # shares the exact same path — see TargetIngestor.note_command_sent. NOT fired
        # on a UI `target.cleared` pool wipe (the on-device list stays populated there).
        try:
            self.ingestor.note_command_sent(port, command)
        except Exception:
            # Delivery already happened. Accounting failure must not report an unsent
            # command and invite a caller to retry a potentially non-idempotent action.
            log.exception("send_to_port: post-delivery accounting failed on %s", port)
        return True
