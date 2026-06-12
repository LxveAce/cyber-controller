"""Resolves available actions for a target based on connected devices and their protocols."""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING

from src.models.action import TargetAction
from src.models.target import Target
from src.protocols import get_protocol_module

if TYPE_CHECKING:
    from src.core.device_manager import DeviceManager

log = logging.getLogger(__name__)


class ActionResolver:
    """Given a target and connected devices, resolves which actions are available.

    The resolver iterates over every connected device, loads its protocol
    module, and checks the module-level ``TARGET_ACTIONS`` dict for entries
    matching the target's :attr:`~Target.target_type`.  Placeholder tokens
    in command templates (``{mac}``, ``{ssid}``, ``{channel}``, ``{rssi}``)
    are substituted from the target's fields.
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
            if matching:
                result[device.port] = [
                    self._render_action(a, target) for a in matching
                ]
        return result

    def _render_action(self, action: TargetAction, target: Target) -> TargetAction:
        """Create a copy of the action with placeholders filled from the target."""
        rendered = copy.deepcopy(action)
        subs = {
            "mac": target.mac,
            "ssid": target.ssid,
            "channel": str(target.channel),
            "rssi": str(target.rssi),
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
            safe_val = val[:64].replace("{", "").replace("}", "")
            result = result.replace("{" + key + "}", safe_val)
        return result


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

    # Send pre-commands (e.g., "select -a 0")
    for pre_cmd in action.pre_commands:
        log.info("Pre-command -> %s: %s", device_port, pre_cmd)
        conn.write(pre_cmd)

    # Send main command
    log.info("Action -> %s: %s", device_port, action.command_template)
    conn.write(action.command_template)

    # Publish event if event_bus provided
    if event_bus and hasattr(event_bus, "publish"):
        event_bus.publish("action.executed", {
            "action": action.name,
            "device": device_port,
            "command": action.command_template,
        })

    return True
