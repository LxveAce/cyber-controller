"""Tests for the pure attack-chain planner (mission_planner.plan_mission).

Exercise condition gating, repeat-count expansion, delay-timeline accumulation, and the plan's
derived views — all without any serial I/O.
"""
from __future__ import annotations

from src.core.mission_planner import MissionContext, plan_mission
from src.models.mission import Mission, StepCondition


def _mission(**kw) -> Mission:
    m = Mission(name=kw.pop("name", "op"), devices=kw.pop("devices", ["COM3"]), **kw)
    return m


# ── happy path ────────────────────────────────────────────────────────────────────────────────────
def test_all_always_steps_run_in_order():
    m = _mission()
    m.add_step("COM3", "scanap", delay_after=1.0)
    m.add_step("COM3", "list -a", delay_after=2.0)
    plan = plan_mission(m)
    assert plan.runnable
    assert [s.seq for s in plan.steps] == [0, 1]
    assert all(s.will_run for s in plan.steps)
    assert len(plan.active_steps) == 2


def test_start_offset_accumulates_only_running_step_delays():
    m = _mission()
    m.add_step("COM3", "a", delay_after=1.0)
    m.add_step("COM3", "b", delay_after=2.0)
    m.add_step("COM3", "c", delay_after=4.0)
    plan = plan_mission(m)
    assert [s.start_offset for s in plan.steps] == [0.0, 1.0, 3.0]
    assert plan.total_delay == 7.0


# ── condition gating ──
def test_device_connected_gates_on_context():
    m = _mission(devices=["COM3", "COM9"])
    m.add_step("COM3", "scanap", condition=StepCondition.DEVICE_CONNECTED)
    m.add_step("COM9", "scanap", condition=StepCondition.DEVICE_CONNECTED)
    plan = plan_mission(m, MissionContext(connected_ports=frozenset({"COM3"})))
    assert plan.steps[0].will_run
    assert not plan.steps[1].will_run
    assert "COM9" in plan.steps[1].skip_reason


def test_device_connected_respects_condition_args_port_override():
    m = _mission(devices=["COM3"])
    step = m.add_step("COM3", "scanap", condition=StepCondition.DEVICE_CONNECTED)
    step.condition_args["port"] = "COM7"  # gate on a different port than device_port
    on_com3 = plan_mission(m, MissionContext(connected_ports=frozenset({"COM3"})))
    on_com7 = plan_mission(m, MissionContext(connected_ports=frozenset({"COM7"})))
    assert not on_com3.steps[0].will_run
    assert on_com7.steps[0].will_run


def test_target_found_and_handshake_gates():
    m = _mission()
    m.add_step("COM3", "deauth", condition=StepCondition.TARGET_FOUND)
    m.add_step("COM3", "crack", condition=StepCondition.HANDSHAKE_CAPTURED)
    none = plan_mission(m, MissionContext())
    assert not none.steps[0].will_run and not none.steps[1].will_run
    both = plan_mission(m, MissionContext(targets_found=True, handshake_captured=True))
    assert both.steps[0].will_run and both.steps[1].will_run


def test_previous_success_skips_when_no_prior_active_step():
    m = _mission()
    # First step gated out (no target), so the PREVIOUS_SUCCESS step has nothing to follow.
    m.add_step("COM3", "a", condition=StepCondition.TARGET_FOUND)
    m.add_step("COM3", "b", condition=StepCondition.PREVIOUS_SUCCESS)
    plan = plan_mission(m, MissionContext())
    assert not plan.steps[0].will_run
    assert not plan.steps[1].will_run
    assert "no prior step" in plan.steps[1].skip_reason


def test_previous_success_runs_when_a_prior_step_is_active():
    m = _mission()
    m.add_step("COM3", "a")  # ALWAYS -> active
    m.add_step("COM3", "b", condition=StepCondition.PREVIOUS_SUCCESS)
    plan = plan_mission(m, MissionContext())
    assert plan.steps[0].will_run and plan.steps[1].will_run


# ── repeat expansion ──
def test_repeat_count_zero_is_one_pass():
    m = _mission(repeat_count=0)
    m.add_step("COM3", "a")
    assert len(plan_mission(m).steps) == 1


def test_repeat_count_expands_passes_and_marks_iteration():
    m = _mission(repeat_count=2)  # 2 additional loops -> 3 passes
    m.add_step("COM3", "a")
    m.add_step("COM3", "b")
    plan = plan_mission(m)
    assert len(plan.steps) == 6
    assert [s.iteration for s in plan.steps] == [0, 0, 1, 1, 2, 2]
    assert [s.step_index for s in plan.steps] == [0, 1, 0, 1, 0, 1]


# ── invalid mission ──
def test_invalid_mission_is_not_runnable_but_still_planned():
    m = Mission(name="")  # invalid: no name, no steps
    plan = plan_mission(m)
    assert not plan.runnable
    assert plan.errors  # carries the validation errors for inspection
    assert plan.steps == ()


def test_default_context_when_omitted():
    m = _mission()
    m.add_step("COM3", "scanap", condition=StepCondition.DEVICE_CONNECTED)
    # No context passed -> empty context -> the connected gate cannot be met.
    assert not plan_mission(m).steps[0].will_run
