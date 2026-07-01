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

This is behavior-preserving: same parts, same wiring, just assembled once in core. See command-center
``cc-rework-PLAN.md`` (stage S2).
"""

from __future__ import annotations

import logging

from src.core.cross_comm import AutoRouter, EventBus, TargetPool
from src.core.device_manager import DeviceManager
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
        # a scan on device A -> target.added -> AutoRouter -> a command on device B. A UI's device tab
        # attaches this ingestor per-connection; the hub just owns the one instance everyone shares.
        self.ingestor = TargetIngestor(self.pool)

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

    def send_to_port(self, port: str, command: str) -> None:
        """Write a command to a connected device (the AutoRouter / Network-tab send sink).

        Pure core: resolve the target device's firmware line terminator (Flipper CR vs LF) before writing,
        so a routed command isn't silently dropped just because that device isn't the focused UI tab, then
        write it (``SerialConnection.write`` rejects embedded control chars). No-ops with a warning when the
        port has no live connection.
        """
        conn = self.dm.get_connection(port)
        if conn and conn.is_connected:
            try:
                dev = self.dm.get_device(port)
                if dev is not None:
                    from src.protocols import line_ending_for
                    conn.line_ending = line_ending_for(dev.firmware or dev.name)
                conn.write(command)  # rejects embedded control chars
            except Exception:
                log.exception("send_to_port %s failed", port)
        else:
            log.warning("send_to_port: no active connection on %s for routed command", port)
