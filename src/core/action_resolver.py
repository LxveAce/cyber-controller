"""Resolves available actions for a target based on connected devices and their protocols."""

from __future__ import annotations

import copy
import logging
import re
from typing import TYPE_CHECKING

from src.models.action import TargetAction
from src.models.target import Target
from src.protocols import get_protocol_module

if TYPE_CHECKING:
    from src.core.device_manager import DeviceManager

log = logging.getLogger(__name__)

# Control chars (C0 + DEL): an over-the-air value (e.g. a scanned SSID) carrying one would trip
# SerialConnection.write()'s injection guard (ValueError) on a {ssid}-bearing action. Strip them here so
# the resolver render path matches the sibling sanitizers (device_tab._sanitize_arg, _sanitize_value).
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ActionResolver:
    """Given a target and connected devices, resolves which actions are available.

    The resolver iterates over every connected device, loads its protocol
    module, and checks the module-level ``TARGET_ACTIONS`` dict for entries
    matching the target's :attr:`~Target.target_type`.  Placeholder tokens
    in command templates (``{mac}``, ``{ssid}``, ``{channel}``, ``{rssi}``,
    ``{index}``) are substituted from the target's fields.  ``{index}``-based
    actions are source-restricted (only offered by the device that discovered
    the target) and dropped when no scan index is known — a scan index is only
    valid for its own device's list.
    """

    def __init__(self, device_manager: DeviceManager) -> None:
        self._dm = device_manager

    def resolve(self, target: Target) -> dict[str, list[TargetAction]]:
        """Return available actions grouped by device port.

        Returns:
            ``{"COM3": [TargetAction, ...], "COM5": [TargetAction, ...]}``
            Only includes devices that have actions for this target type.
        """
        result: dict[str, list[TargetAction]] = {}
        for device in self._dm.list_connected():
            protocol_mod = get_protocol_module(device.firmware or device.name)
            if protocol_mod is None:
                continue
            actions = getattr(protocol_mod, "TARGET_ACTIONS", {})
            matching = actions.get(target.target_type, [])
            usable = [a for a in matching if self._action_applicable(a, device, target)]
            if usable:
                result[device.port] = [
                    self._render_action(a, target) for a in usable
                ]
        return result

    @staticmethod
    def _uses_index(action: TargetAction) -> bool:
        """True if the action depends on a per-device scan index ({index} in template or a pre-command)."""
        return "{index}" in action.command_template or any(
            "{index}" in c for c in action.pre_commands
        )

    def _action_applicable(self, action: TargetAction, device: object, target: Target) -> bool:
        """Index-based actions are only valid when (a) we actually know this target's scan index and (b) the
        device offering the action is the one that discovered the target — a scan index is meaningful only to
        its own device's list, so firing it cross-device (or with no index) could select the WRONG AP. Without
        a known index we drop the action rather than send a literal/guessed ``{index}`` (lab-safety)."""
        if not self._uses_index(action):
            return True
        extra = getattr(target, "extra", None) or {}
        if extra.get("index") in (None, ""):
            return False
        return getattr(device, "port", None) == getattr(target, "device_source", None)

    def _render_action(self, action: TargetAction, target: Target) -> TargetAction:
        """Create a copy of the action with placeholders filled from the target."""
        rendered = copy.deepcopy(action)
        extra = getattr(target, "extra", None) or {}
        subs = {
            "mac": target.mac,
            "ssid": target.ssid,
            "channel": str(target.channel),
            "rssi": str(target.rssi),
            "index": str(extra.get("index", "")),
        }
        rendered.command_template = self._safe_sub(action.command_template, subs)
        rendered.pre_commands = [self._safe_sub(c, subs) for c in action.pre_commands]
        return rendered

    @staticmethod
    def _safe_sub(template: str, subs: dict[str, str]) -> str:
        """Substitute placeholders safely (no format string injection).

        Each substitution value is capped at 64 characters and stripped of
        ``{`` / ``}`` to prevent recursive expansion or injection.
        """
        result = template
        for key, val in subs.items():
            # Strip control chars first, then cap, then remove braces — keeps the value safe for both
            # the serial injection guard and recursive-expansion.
            safe_val = _CTRL_RE.sub("", val)[:64].replace("{", "").replace("}", "")
            result = result.replace("{" + key + "}", safe_val)
        return result


def _echo_routed(device_port: str, cmd: str) -> None:
    """Mirror a routed/AutoRouter serial write into the app-wide activity bus so the always-visible
    bottom terminal echoes programmatic sends too — not just hand-typed ones. Best-effort and fully
    guarded: this is a core module that also runs headless/in tests, so a missing PyQt or absent GUI
    must never affect whether the command was sent. See src/core/activity_log.py."""
    if not cmd:
        return
    try:
        from src.core.activity_log import activity_log
        activity_log().emit_line("route", f"[{device_port}] > {cmd}")
    except Exception:  # noqa: BLE001 — echoing must never break the send path
        pass


def execute_action(
    action: TargetAction,
    device_port: str,
    device_manager: DeviceManager,
    event_bus: object | None = None,
) -> bool:
    """Execute a target action on a specific device.

    Sends pre_commands first (e.g., select AP), then the main command.
    Returns ``True`` if the command was sent successfully.

    Args:
        action: The resolved :class:`TargetAction` to execute.
        device_port: Serial port of the device to send commands to.
        device_manager: Active :class:`DeviceManager` instance.
        event_bus: Optional :class:`EventBus` to publish an
            ``action.executed`` event on success.
    """
    conn = device_manager.get_connection(device_port)
    if conn is None:
        log.warning("No active connection on %s", device_port)
        return False

    # Stamp the firmware terminator (Flipper CR vs LF) so a routed action on a non-active device's
    # connection still executes instead of being silently dropped by a CR-only shell.
    dev = device_manager.get_device(device_port)
    if dev is not None:
        try:
            from src.protocols import line_ending_for
            conn.line_ending = line_ending_for(dev.firmware or dev.name)
        except Exception:
            pass

    # Send pre-commands (e.g., "select -a 0")
    for pre_cmd in action.pre_commands:
        log.info("Pre-command -> %s: %s", device_port, pre_cmd)
        conn.write(pre_cmd)
        _echo_routed(device_port, pre_cmd)

    # Send main command
    log.info("Action -> %s: %s", device_port, action.command_template)
    conn.write(action.command_template)
    _echo_routed(device_port, action.command_template)

    # Publish event if event_bus provided
    if event_bus and hasattr(event_bus, "publish"):
        event_bus.publish("action.executed", {
            "action": action.name,
            "device": device_port,
            "command": action.command_template,
        })

    return True
