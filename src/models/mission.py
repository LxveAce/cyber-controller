"""Mission model — orchestrated multi-device operation sequences."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class StepCondition(Enum):
    """Conditions that gate whether a mission step executes."""

    ALWAYS = "always"
    DEVICE_CONNECTED = "device_connected"
    TARGET_FOUND = "target_found"
    PREVIOUS_SUCCESS = "previous_success"
    HANDSHAKE_CAPTURED = "handshake_captured"


class MissionStatus(Enum):
    """Overall mission lifecycle state."""

    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass
class MissionStep:
    """A single step within a mission.

    Attributes:
        device_port: Serial port of the target device.
        command: Command string to send.
        delay_after: Seconds to wait after sending the command.
        condition: Gate condition that must be satisfied before executing.
        condition_args: Extra arguments for the condition check.
        timeout: Max seconds to wait for the step to complete.
        description: Human-readable step summary.
    """

    device_port: str
    command: str
    delay_after: float = 0.0
    condition: StepCondition = StepCondition.ALWAYS
    condition_args: dict[str, Any] = field(default_factory=dict)
    timeout: float = 30.0
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "device_port": self.device_port,
            "command": self.command,
            "delay_after": self.delay_after,
            "condition": self.condition.value,
            "condition_args": self.condition_args,
            "timeout": self.timeout,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict) -> MissionStep:
        data = dict(data)
        data["condition"] = StepCondition(data.get("condition", "always"))
        return cls(**data)


@dataclass
class Mission:
    """A reusable, multi-device operation plan.

    Attributes:
        name: Mission identifier.
        description: What this mission accomplishes.
        devices: List of required device port strings.
        steps: Ordered list of MissionStep objects.
        created_at: Creation timestamp (UTC).
        status: Current lifecycle state.
        tags: Arbitrary labels.
        repeat_count: How many times to loop the step list (0 = once).
    """

    name: str
    description: str = ""
    devices: list[str] = field(default_factory=list)
    steps: list[MissionStep] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: MissionStatus = MissionStatus.DRAFT
    tags: list[str] = field(default_factory=list)
    repeat_count: int = 0

    def add_step(
        self,
        device_port: str,
        command: str,
        *,
        delay_after: float = 0.0,
        condition: StepCondition = StepCondition.ALWAYS,
        description: str = "",
    ) -> MissionStep:
        """Create and append a new step."""
        step = MissionStep(
            device_port=device_port,
            command=command,
            delay_after=delay_after,
            condition=condition,
            description=description,
        )
        self.steps.append(step)
        return step

    def validate(self) -> list[str]:
        """Return a list of validation errors (empty = valid)."""
        errors: list[str] = []
        if not self.name.strip():
            errors.append("Mission name is required.")
        if not self.steps:
            errors.append("Mission must have at least one step.")
        for i, step in enumerate(self.steps):
            if step.device_port not in self.devices:
                errors.append(
                    f"Step {i}: device_port '{step.device_port}' not in mission devices list."
                )
            if not step.command.strip():
                errors.append(f"Step {i}: command is empty.")
        return errors

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "devices": self.devices,
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at.isoformat(),
            "status": self.status.value,
            "tags": self.tags,
            "repeat_count": self.repeat_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Mission:
        data = dict(data)
        data["steps"] = [MissionStep.from_dict(s) for s in data.get("steps", [])]
        data["status"] = MissionStatus(data.get("status", "draft"))
        if isinstance(data.get("created_at"), str):
            data["created_at"] = datetime.fromisoformat(data["created_at"])
        return cls(**data)
