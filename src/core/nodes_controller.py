"""Nodes controller — UI-agnostic surface over node_provision + DeviceManager for the W1.1 Nodes tab.

All four frontends (qt / tk / tui / web) bind to THIS rather than poking ``node_provision`` and
``DeviceManager`` directly, so the security-sensitive rules live in one tested place:

  * **Gated:** every operation goes through the access-gate vault and fails CLOSED when the gate is locked
    (``VaultLockedError``) — no node op is possible without unlocking.
  * **Key-free:** nothing this returns or logs ever carries key bytes. Rows expose only ``node_id``,
    ``label``, ``role``, the ``tx_epoch``/``rx_epoch`` cursors, and live ``connected``/``attached`` status.
  * **Attach = present a node as a managed device:** :meth:`attach` opens a crash-safe ``NodeLink`` (epoch
    reserved by ``node_provision``) over a caller-supplied gateway and registers it via
    ``DeviceManager.attach_connection`` — so a wireless node then behaves exactly like a wired one
    everywhere downstream. :meth:`detach` persists the replay-window head before teardown.

A "mask" is just a named node identity/label; :data:`DEFAULT_MASKS` is a small suggestion catalogue the UI
can offer — the real catalogue is whatever the owner names their nodes.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from src.core import node_provision
from src.core.node_link import NodeLink
from src.models.device import BoardType, Device

log = logging.getLogger(__name__)

# Suggested node identity presets (purely cosmetic labels — no secrets, owner may use anything).
DEFAULT_MASKS: tuple[str, ...] = ("pager", "scanner", "relay", "sensor", "beacon")

__all__ = ["NodesController", "AlreadyAttachedError", "DEFAULT_MASKS"]


class AlreadyAttachedError(Exception):
    """Refusing to attach a node that already has a live link (detach it first)."""


class NodesController:
    """View-model/orchestration over provisioned wireless nodes. Construct with the app's
    :class:`DeviceManager`; ``vault_getter`` defaults to the gate-keyed vault and is injectable for tests."""

    # Persist the anti-replay head at most once per this many accepted inbound frames. A vault write per
    # frame would be far too chatty on the RX thread; this bounds post-crash replay exposure to <N frames.
    _RX_PERSIST_EVERY = 8

    def __init__(
        self,
        device_manager: Any,
        *,
        vault_getter: Optional[Callable[[], Any]] = None,
        owner: str = "nodes",
    ) -> None:
        self._dm = device_manager
        self._owner = owner
        self._vault_getter = vault_getter or node_provision.current_vault
        self._links: dict[int, NodeLink] = {}
        self._lock = threading.RLock()   # guards the attach/detach check-and-set on _links

    # ── gate state ───────────────────────────────────────────────────
    def is_unlocked(self) -> bool:
        """True if the access gate is unlocked (node ops are possible). Never raises."""
        try:
            self._vault_getter()
            return True
        except node_provision.VaultLockedError:
            return False

    def _vault(self) -> Any:
        """The unlocked vault, or raise ``VaultLockedError`` (fail closed)."""
        return self._vault_getter()

    # ── read ─────────────────────────────────────────────────────────
    def list_rows(self) -> list[dict]:
        """Key-free rows for the table: provisioning summary + live connected/attached state."""
        rows = node_provision.list_nodes(self._vault())
        for r in rows:
            # Read the ACTUAL port off the live link when attached (a caller may have set port=…),
            # so connected-state lookup can't silently miss; fall back to the default convention.
            link = self._links.get(r["node_id"])
            port = link.port if link is not None else f"node:{r['node_id']}"
            dev = self._dm.get_device(port)
            r["port"] = port
            r["connected"] = bool(getattr(dev, "connected", False)) if dev is not None else False
            r["attached"] = link is not None
        return rows

    def masks(self) -> list[str]:
        """Suggested identity labels for the UI (the catalogue is owner-defined)."""
        return list(DEFAULT_MASKS)

    # ── provisioning (gated, key-free returns) ───────────────────────
    def provision(self, node_id: int, *, role: str = "host", label: str = "") -> dict:
        return node_provision.provision_node(self._vault(), node_id, role=role, label=label)

    def rotate(self, node_id: int) -> dict:
        return node_provision.rotate_key(self._vault(), node_id)

    def deprovision(self, node_id: int) -> bool:
        # A live link to a node we're deleting would outlive its key — tear it down first.
        if node_id in self._links:
            self.detach(node_id)
        return node_provision.deprovision_node(self._vault(), node_id)

    # ── attach / detach as a managed device ──────────────────────────
    def attach(self, node_id: int, gateway: Any, *, label: Optional[str] = None, **link_kw: Any) -> NodeLink:
        """Open a crash-safe NodeLink for *node_id* over *gateway* and register it as a managed device.

        Reserves an epoch (via ``node_provision.open_node_link``) so a restart can't reuse a nonce. Raises
        :class:`AlreadyAttachedError` if the node already has a live link.
        """
        # Root self-attach-loop guard — identity, not naming: a NodeLink (even one with a custom port that
        # doesn't start with "node:") must never become another node's gateway.
        if isinstance(gateway, NodeLink):
            raise ValueError("a NodeLink cannot be a gateway for another node (no self-attach loop)")
        with self._lock:
            if node_id in self._links:
                raise AlreadyAttachedError(f"node {node_id} is already attached; detach first")
            vault = self._vault()
            if label is None:
                rec = next((r for r in node_provision.list_nodes(vault) if r["node_id"] == node_id), None)
                label = (rec or {}).get("label") or f"node {node_id}"
            link = node_provision.open_node_link(vault, node_id, gateway, **link_kw)
            device = Device(
                port=link.port,
                name=label,
                firmware="node",
                board_type=BoardType.UNKNOWN,
                connected=bool(link.is_connected),
            )
            self._dm.attach_connection(device, link, owner=self._owner)
            self._links[node_id] = link
            # Register this node as an owner of the GATEWAY's real port so the gateway refcount keeps the
            # dongle alive until the last node detaches. Without this the node's ownership lives only under
            # its synthetic "node:<id>" key, so the gateway's DIRECT owner (e.g. the Devices tab)
            # disconnecting would physically close the shared dongle out from under this attached node.
            gw_port = link.gateway_port
            if gw_port and gw_port != link.port:
                self._dm.add_connection_owner(gw_port, self._gateway_owner_tag(node_id))
            # Persist the anti-replay head AS the session runs (throttled), not only at clean detach — a
            # crash between attach and detach would otherwise roll the head back to its last saved value and
            # let every node->host frame captured since then replay. Bounded exposure = _RX_PERSIST_EVERY.
            link.on_rx_advance(self._make_rx_persister(node_id, link))
        log.info("attached node %s as %s", node_id, link.port)  # no key material
        return link

    @staticmethod
    def _gateway_owner_tag(node_id: int) -> str:
        """The owner tag a node uses when borrowing its gateway's refcount (distinct from any panel owner
        like 'devices_tab', so releasing one never drops the other)."""
        return f"node:{node_id}"

    def _make_rx_persister(self, node_id: int, link: NodeLink) -> Callable[[], None]:
        """Build the throttled on_rx_advance callback for a link: persist the replay head every
        _RX_PERSIST_EVERY accepted frames. Runs on the RX thread, so it stays cheap and never raises."""
        state = {"n": 0}

        def _persist() -> None:
            state["n"] += 1
            if state["n"] % self._RX_PERSIST_EVERY:
                return
            try:
                node_provision.persist_rx_state(self._vault(), node_id, link)
            except node_provision.VaultLockedError:
                pass  # locked mid-session; a later accepted frame (or detach) retries
            except Exception:  # noqa: BLE001 — never let persistence break the RX path
                log.debug("periodic replay-head persist failed for node %s", node_id, exc_info=True)

        return _persist

    def available_gateways(self) -> list[dict]:
        """Connectable gateways for the attach picker: CONNECTED DeviceManager devices with a live
        connection that are NOT themselves node links (a node link can't gateway a node — no self-attach
        loop). Key-free — only port/name. One dongle may gateway several nodes, so reuse is allowed."""
        out: list[dict] = []
        for dev in self._dm.list_devices():
            port = getattr(dev, "port", "")
            if not getattr(dev, "connected", False):
                continue
            conn = self._dm.get_connection(port)
            if conn is None:
                continue
            if isinstance(conn, NodeLink) or port.startswith("node:"):
                continue   # a node link can't gateway a node — identity check, not just the naming convention
            out.append({"port": port, "name": getattr(dev, "name", "") or port})
        return out

    def attach_via_port(self, node_id: int, gateway_port: str, **link_kw: Any) -> NodeLink:
        """Attach *node_id* over the live connection at *gateway_port* (looked up in DeviceManager).

        Guards against using a node link as a gateway (no self-attach loop) and against a dead port before
        delegating to :meth:`attach` (which gate-checks and reserves the epoch)."""
        if gateway_port.startswith("node:"):
            raise ValueError("a node link cannot be used as a gateway")
        conn = self._dm.get_connection(gateway_port)
        if conn is None:
            raise ValueError(f"no live connection on {gateway_port!r}")
        # attach() has the authoritative identity guard; this catches a custom-port node link early too.
        return self.attach(node_id, conn, **link_kw)

    def detach(self, node_id: int) -> bool:
        """Persist the replay head, close the link (detach from gateway), and unregister the device.

        Teardown (close + unregister) ALWAYS completes even if persistence or close fails — otherwise a
        stale link would keep decrypting inbound frames and a phantom device would linger for the router.
        """
        with self._lock:
            link = self._links.pop(node_id, None)
        if link is None:
            return False
        link.on_rx_advance(None)  # stop the periodic persister firing during/after teardown
        # Release this node's borrow-ownership of the gateway port (may close the dongle if it was the last
        # owner; keeps it open while its direct owner or another node still holds it). Never blocks teardown.
        gw_port = link.gateway_port
        if gw_port and gw_port != link.port:
            try:
                self._dm.close_connection(gw_port, owner=self._gateway_owner_tag(node_id))
            except Exception:  # noqa: BLE001 — teardown must always continue
                log.debug("could not release gateway ownership for node %s", node_id, exc_info=True)
        # Best-effort persist of the replay head — must never block teardown.
        try:
            node_provision.persist_rx_state(self._vault(), node_id, link)
        except node_provision.VaultLockedError:
            log.warning("gate locked at detach; replay-window head for node %s not persisted", node_id)
        except Exception:  # e.g. the node was deprovisioned by another process mid-session
            log.warning("could not persist replay head for node %s; tearing down anyway", node_id)
        # Teardown always runs.
        try:
            link.close()
        except Exception:
            log.warning("error closing link for node %s; unregistering anyway", node_id)
        self._dm.remove_device(link.port)
        log.info("detached node %s", node_id)
        return True

    def attached_ids(self) -> list[int]:
        return sorted(self._links)
