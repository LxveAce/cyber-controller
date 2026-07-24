"""Model tests for the Mission scaffolding (previously dead + untested, now built out).

Covers validation, add_step, and dict round-tripping — the foundation the planner rests on.
"""
from __future__ import annotations

from src.models.mission import Mission, MissionStatus, MissionStep, StepCondition


def test_add_step_appends_and_returns():
    m = Mission(name="recon")
    step = m.add_step("COM3", "scanap", delay_after=1.5, description="find APs")
    assert m.steps == [step]
    assert step.device_port == "COM3" and step.command == "scanap"
    assert step.delay_after == 1.5
    assert step.condition is StepCondition.ALWAYS


def test_validate_flags_missing_name_and_no_steps():
    errors = Mission(name="   ").validate()
    assert any("name is required" in e for e in errors)
    assert any("at least one step" in e for e in errors)


def test_validate_flags_step_port_not_in_devices():
    m = Mission(name="op", devices=["COM3"])
    m.add_step("COM9", "scanap")  # COM9 not declared in devices
    errors = m.validate()
    assert any("not in mission devices" in e for e in errors)


def test_validate_flags_empty_command():
    m = Mission(name="op", devices=["COM3"])
    m.add_step("COM3", "   ")
    assert any("command is empty" in e for e in m.validate())


def test_valid_mission_has_no_errors():
    m = Mission(name="op", devices=["COM3"])
    m.add_step("COM3", "scanap")
    assert m.validate() == []


def test_dict_round_trip_preserves_everything():
    m = Mission(name="op", description="d", devices=["COM3"], tags=["lab"], repeat_count=2)
    m.add_step("COM3", "scanap", condition=StepCondition.TARGET_FOUND, description="s")
    m.status = MissionStatus.READY

    restored = Mission.from_dict(m.to_dict())

    assert restored.name == m.name
    assert restored.description == m.description
    assert restored.devices == m.devices
    assert restored.tags == m.tags
    assert restored.repeat_count == m.repeat_count
    assert restored.status is MissionStatus.READY
    assert len(restored.steps) == 1
    assert restored.steps[0].condition is StepCondition.TARGET_FOUND
    assert restored.steps[0].command == "scanap"


def test_step_from_dict_defaults_condition_to_always():
    step = MissionStep.from_dict({"device_port": "COM3", "command": "scanap"})
    assert step.condition is StepCondition.ALWAYS
