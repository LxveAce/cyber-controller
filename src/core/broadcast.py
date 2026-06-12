"""Unified Action Broadcast — one intent, every connected radio, each in its own native command.

This is the "conductor" layer above the per-device protocol parsers. The user picks one verb
("Find APs", "Deauth All", "BLE Scan", ...) and the engine translates it into each connected
firmware's *native* command and fires them all at once. Results converge for FREE: each device's
serial replies are parsed by its own ``BaseProtocol.parse_line()`` and pushed into the shared
``TargetPool`` by the already-wired ``TargetIngestor`` — so this engine only handles DISPATCH.

Design notes:
- A :class:`BroadcastVerb` is firmware-agnostic. The translation lives in a per-protocol module
  dict ``BROADCAST_CAPABILITIES`` (``verb -> (pre_commands, command)``), resolved by name via the
  existing ``get_protocol_module()`` — the same idiom ``ActionResolver`` uses for ``TARGET_ACTIONS``.
  A firmware that omits a verb is SKIPPED + reported (never a silent drop).
- Safety is per the project guardrails: every concrete command is classified via
  ``safety.classify``; the whole fan-out is gated ONCE via ``safety.worst_of`` + ``should_confirm``
  (label/warn, never block). ``STOP_ALL`` is always safe and never gated.
- No Qt here — pure logic + thin serial egress, unit-testable like ``safety``/``cross_comm``.
- Import hygiene: this module imports only ``src.models.*`` at top; protocol modules import
  ``BroadcastVerb`` from here; the engine imports ``src.protocols`` LAZILY inside its methods
  (exactly how ``action_resolver`` avoids a cycle).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from src.models.action import ActionCategory
from src.models.target import TargetType

log = logging.getLogger(__name__)


class BroadcastVerb(Enum):
    """Stable identity for each high-level broadcast action."""
    FIND_APS = "find_aps"
    SCAN_STATIONS = "scan_stations"
    BLE_SCAN = "ble_scan"
    SUBGHZ_SCAN = "subghz_scan"
    CAPTURE_HANDSHAKES = "capture_handshakes"
    DEAUTH_ALL = "deauth_all"
    BEACON_SPAM = "beacon_spam"
    BLE_SPAM = "ble_spam"
    MESH_RELAY = "mesh_relay"
    STOP_ALL = "stop_all"  # universal kill-switch, always safe, never gated


@dataclass(frozen=True)
class BroadcastAction:
    """A high-level verb shown as one button; firmware-agnostic."""
    verb: BroadcastVerb
    label: str
    icon: str
    category: ActionCategory
    produces: TargetType | None = None
    base_danger: str = ""  # baseline if a firmware doesn't annotate; real call still classifies
    description: str = ""


@dataclass(frozen=True)
class ConcreteCommand:
    """One firmware's native realization of a BroadcastAction on one device."""
    port: str
    firmware: str
    pre_commands: tuple[str, ...]
    command: str
    danger: str


@dataclass
class BroadcastPlan:
    """The full per-device plan for one broadcast, BEFORE anything is sent."""
    action: BroadcastAction
    concrete: list[ConcreteCommand] = field(default_factory=list)
    skipped: list[tuple[str, str, str]] = field(default_factory=list)  # (port, firmware, reason)

    @property
    def worst_danger(self) -> str:
        from src.core import safety
        return safety.worst_of(*(c.danger for c in self.concrete))


@dataclass
class BroadcastResult:
    """Outcome of dispatching one device's concrete command."""
    port: str
    firmware: str
    command: str
    status: str  # "sent" | "failed" | "skipped"
    detail: str = ""


# --- The frozen registry: the single source for the UI button row ---

_I_SCAN, _I_STA, _I_BLE, _I_SUB = "\U0001F4E1", "\U0001F465", "\U0001F537", "\U0001F4FB"
_I_CAP, _I_DEAUTH, _I_BEACON, _I_SPAM, _I_MESH, _I_STOP = (
    "\U0001F4BE", "⚡", "\U0001F4E2", "\U0001F4F6", "\U0001F578", "\U0001F6D1")

BROADCAST_ACTIONS: dict[BroadcastVerb, BroadcastAction] = {
    BroadcastVerb.FIND_APS: BroadcastAction(
        BroadcastVerb.FIND_APS, "Find APs", _I_SCAN, ActionCategory.SCAN, TargetType.AP,
        description="Scan for WiFi access points on every capable radio at once."),
    BroadcastVerb.SCAN_STATIONS: BroadcastAction(
        BroadcastVerb.SCAN_STATIONS, "Scan Stations", _I_STA, ActionCategory.SCAN, TargetType.CLIENT,
        description="Scan for WiFi client stations."),
    BroadcastVerb.BLE_SCAN: BroadcastAction(
        BroadcastVerb.BLE_SCAN, "BLE Scan", _I_BLE, ActionCategory.SCAN, TargetType.BLE,
        description="Scan for Bluetooth Low Energy devices."),
    BroadcastVerb.SUBGHZ_SCAN: BroadcastAction(
        BroadcastVerb.SUBGHZ_SCAN, "SubGHz Scan", _I_SUB, ActionCategory.SCAN, TargetType.SUBGHZ,
        description="Scan the Sub-GHz spectrum (CC1101-equipped radios)."),
    BroadcastVerb.CAPTURE_HANDSHAKES: BroadcastAction(
        BroadcastVerb.CAPTURE_HANDSHAKES, "Capture Handshakes", _I_CAP, ActionCategory.CAPTURE,
        description="Start WPA handshake / PMKID capture."),
    BroadcastVerb.DEAUTH_ALL: BroadcastAction(
        BroadcastVerb.DEAUTH_ALL, "Deauth All", _I_DEAUTH, ActionCategory.ATTACK,
        base_danger="lab-only", description="Deauth every scanned AP (controlled-lab only)."),
    BroadcastVerb.BEACON_SPAM: BroadcastAction(
        BroadcastVerb.BEACON_SPAM, "Beacon Spam", _I_BEACON, ActionCategory.ATTACK,
        base_danger="lab-only", description="Flood fake beacon frames (controlled-lab only)."),
    BroadcastVerb.BLE_SPAM: BroadcastAction(
        BroadcastVerb.BLE_SPAM, "BLE Spam", _I_SPAM, ActionCategory.ATTACK,
        base_danger="lab-only", description="BLE advertisement spam (controlled-lab only)."),
    BroadcastVerb.MESH_RELAY: BroadcastAction(
        BroadcastVerb.MESH_RELAY, "Mesh Status", _I_MESH, ActionCategory.UTILITY,
        description="Query mesh nodes (Meshtastic)."),
    BroadcastVerb.STOP_ALL: BroadcastAction(
        BroadcastVerb.STOP_ALL, "STOP ALL", _I_STOP, ActionCategory.UTILITY,
        description="Stop the current operation on every device. Always safe."),
}


class BroadcastEngine:
    """Resolves a verb to per-device native commands and fans them out simultaneously."""

    def __init__(self, device_manager: Any, event_bus: Any,
                 action_registry: dict[BroadcastVerb, BroadcastAction] | None = None) -> None:
        self._dm = device_manager
        self._bus = event_bus
        self._actions = action_registry or BROADCAST_ACTIONS

    # ── plan (no side effects) ───────────────────────────────────────
    def plan(self, verb: BroadcastVerb) -> BroadcastPlan:
        from src.core import safety
        from src.protocols import get_protocol, get_protocol_module

        action = self._actions[verb]
        plan = BroadcastPlan(action=action)
        for device in self._dm.list_connected():
            fw = (getattr(device, "firmware", "") or "").strip()
            port = getattr(device, "port", "?")
            if not fw:
                plan.skipped.append((port, "(unknown)", "firmware unknown"))
                continue
            mod = get_protocol_module(fw)
            caps = getattr(mod, "BROADCAST_CAPABILITIES", {}) if mod else {}
            if not mod:
                plan.skipped.append((port, fw, "firmware unknown"))
                continue
            if verb not in caps:
                plan.skipped.append((port, fw, "unsupported by this firmware"))
                continue
            pre, cmd = caps[verb]
            info = self._command_info_for(get_protocol(fw), cmd)
            danger = safety.classify(cmd, info)
            plan.concrete.append(ConcreteCommand(port, fw, tuple(pre), cmd, danger))
        return plan

    def available_verbs(self) -> dict[BroadcastVerb, int]:
        """verb -> count of connected devices that support it (for live button enable)."""
        return {v: len(self.plan(v).concrete) for v in self._actions}

    # ── dispatch (true simultaneous fan-out) ─────────────────────────
    def dispatch(self, plan: BroadcastPlan, confirmed: bool = False) -> list[BroadcastResult]:
        """Send a pre-approved plan. If the plan is dangerous and not confirmed, sends nothing
        and returns a single needs-confirmation sentinel (the UI owns the dialog)."""
        if plan.worst_danger and not confirmed:
            return [BroadcastResult("", "", plan.action.label, "needs-confirm", plan.worst_danger)]

        self._publish("broadcast.started", {
            "verb": plan.action.verb.value, "label": plan.action.label,
            "count": len(plan.concrete),
            "skipped": [{"port": p, "firmware": f, "reason": r} for p, f, r in plan.skipped],
        })

        results: list[BroadcastResult] = []
        lock = threading.Lock()

        def _send(cc: ConcreteCommand) -> None:
            conn = self._dm.get_connection(cc.port)
            if conn is None:
                res = BroadcastResult(cc.port, cc.firmware, cc.command, "failed", "no active connection")
            else:
                try:
                    for pre in cc.pre_commands:
                        conn.write(pre)
                    conn.write(cc.command)
                    res = BroadcastResult(cc.port, cc.firmware, cc.command, "sent")
                except Exception as exc:  # isolate one device's failure from the rest
                    res = BroadcastResult(cc.port, cc.firmware, cc.command, "failed", str(exc))
            with lock:
                results.append(res)
            self._publish("action.executed", {
                "port": cc.port, "firmware": cc.firmware, "command": cc.command,
                "status": res.status, "detail": res.detail, "source": "broadcast",
            })

        threads = [threading.Thread(target=_send, args=(cc,), daemon=True) for cc in plan.concrete]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        sent = sum(1 for r in results if r.status == "sent")
        self._publish("broadcast.completed", {
            "verb": plan.action.verb.value, "label": plan.action.label,
            "sent": sent, "failed": len(results) - sent, "skipped": len(plan.skipped),
        })
        return results

    # ── helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _command_info_for(proto: Any, cmd: str):
        """Match *cmd* to one of the protocol's CommandInfo entries (exact, then prefix) so the
        authoritative ``CommandInfo.danger`` is used. Mirrors device_tab._command_info."""
        try:
            cmds = proto.get_commands()
        except Exception:
            return None
        for ci in cmds:
            if ci.name == cmd:
                return ci
        for ci in cmds:
            if cmd.startswith(ci.name):
                return ci
        return None

    def _publish(self, topic: str, payload: dict) -> None:
        pub = getattr(self._bus, "publish", None)
        if callable(pub):
            try:
                pub(topic, payload)
            except Exception:
                log.debug("broadcast bus publish failed", exc_info=True)
