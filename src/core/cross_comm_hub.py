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
        ingestor: Feeds each connected device's parsed serial output into the pool.
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

        # Feeds parsed APs/clients from each connected device into the shared pool, completing the loop:
        # a scan on device A -> target.added -> AutoRouter -> a command on device B. The hub owns the one
        # instance everyone shares AND auto-attaches it to every connection the moment it opens (below).
        self.ingestor = TargetIngestor(self.pool)

        # Attach the ingestor to EVERY connection the DeviceManager opens — Devices-tab Connect, Wardrive,
        # Broadcast, or an injected NodeLink — so a scan on ANY opened device feeds the pool, not only a
        # Devices-tab Connect (which was the sole attach site before, leaving the Targets tab empty during
        # wardriving/broadcasting). Re-attach is idempotent (TargetIngestor dedups per port).
        self.dm.on_connection_opened(self._attach_ingestor)

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
        except Exception:
            log.exception("send_to_port %s failed", port)
