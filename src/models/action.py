"""Action model — represents an executable action against a discovered target."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ActionCategory(Enum):
    """Categories of target actions."""

    ATTACK = "attack"
    SCAN = "scan"
    CAPTURE = "capture"
    MONITOR = "monitor"
    UTILITY = "utility"


@dataclass
class TargetAction:
    """An action that can be performed on a target by a specific protocol.

    Attributes:
        name: Human-readable action name (e.g. "Deauth", "Beacon Clone").
        command_template: Serial command with optional placeholders
            (e.g. "attack -t deauth" or "deauth {mac}").
        description: Human-readable description of what the action does.
        category: Functional category of the action.
        requires_selection: Whether the target must be "selected" first
            on the device (e.g. Marauder's ``select -a`` step).
        pre_commands: Commands to run before the main command
            (e.g. ``["select -a {index}"]``).
        chain_events: Event types this action might produce on the
            EventBus, enabling downstream automation.
    """

    name: str
    command_template: str
    description: str
    category: ActionCategory = ActionCategory.ATTACK
    requires_selection: bool = False
    pre_commands: list[str] = field(default_factory=list)
    chain_events: list[str] = field(default_factory=list)
