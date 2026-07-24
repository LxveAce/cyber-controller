"""Tests for :mod:`src.core.mission_runner` — the side-effecting mission executor.

The runner is exercised with an injected fake sender (so nothing touches hardware), a recording
sleep, and — for most cases — an injected classifier so danger is deterministic and decoupled from
safety.py's keyword list. Two tests use the REAL default classifier to prove the safety wiring.
"""
from __future__ import annotations

import pytest

from src.core.mission_planner import MissionContext, plan_mission
from src.core.mission_runner import (
    MissionRefused,
    MissionRunner,
    StepStatus,
    device_manager_sender,
    format_plan,
    run_mission,
)
from src.models.mission import Mission, MissionStep, StepCondition


class RecordingSender:
    """Records every (port, command) sent; optionally raises for a chosen command."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self._fail_on = fail_on

    def __call__(self, port: str, command: str) -> None:
        if command == self._fail_on:
            raise RuntimeError(f"boom on {command}")
        self.sent.append((port, command))


def _mission(*steps: MissionStep, name: str = "m", devices: list[str] | None = None) -> Mission:
    ports = devices if devices is not None else sorted({s.device_port for s in steps})
    return Mission(name=name, devices=ports, steps=list(steps))


def _run(mission, *, context=None, armed=(), classify=None):
    sends = RecordingSender()
    naps: list[float] = []
    runner = MissionRunner(
        sends,
        classify=classify if classify is not None else (lambda cmd: ""),
        is_armed=lambda p: p in set(armed),
        sleep=naps.append,
    )
    run = runner.run(plan_mission(mission, context))
    return run, sends, naps


# ── happy path ───────────────────────────────────────────────────────

def test_sends_active_steps_in_order_and_waits():
    mission = _mission(
        MissionStep("COM3", "scan", delay_after=0.5),
        MissionStep("COM3", "list", delay_after=1.0),
    )
    run, sends, naps = _run(mission)
    assert [r.status for r in run.results] == [StepStatus.SENT, StepStatus.SENT]
    assert sends.sent == [("COM3", "scan"), ("COM3", "list")]
    assert naps == [0.5, 1.0]
    assert run.ok


def test_zero_delay_step_does_not_sleep():
    mission = _mission(MissionStep("COM3", "scan", delay_after=0.0))
    _run_result, _sends, naps = _run(mission)
    assert naps == []


# ── refusing a non-runnable plan ─────────────────────────────────────

def test_non_runnable_plan_is_refused_and_sends_nothing():
    # device_port not in the mission's devices list -> validate() error -> not runnable.
    mission = Mission(name="bad", devices=["COM3"], steps=[MissionStep("COM9", "scan")])
    plan = plan_mission(mission)
    assert not plan.runnable
    sends = RecordingSender()
    runner = MissionRunner(sends)
    with pytest.raises(MissionRefused):
        runner.run(plan)
    assert sends.sent == []


def test_empty_mission_is_refused():
    plan = plan_mission(Mission(name="empty", devices=[], steps=[]))
    with pytest.raises(MissionRefused):
        MissionRunner(RecordingSender()).run(plan)


# ── the SAFE/ARMED safety gate ───────────────────────────────────────

def test_offensive_verb_refused_on_unarmed_port():
    mission = _mission(
        MissionStep("COM3", "scan"),
        MissionStep("COM3", "deauth 0"),
    )
    run, sends, _naps = _run(
        mission, classify=lambda cmd: "lab-only" if "deauth" in cmd else ""
    )
    assert [r.status for r in run.results] == [StepStatus.SENT, StepStatus.REFUSED_UNSAFE]
    # The dangerous command NEVER reached the port.
    assert sends.sent == [("COM3", "scan")]
    assert run.refused[0].command == "deauth 0"
    # A refusal is a safety outcome, not a failure.
    assert run.ok


def test_offensive_verb_runs_when_port_armed():
    mission = _mission(MissionStep("COM3", "deauth 0"))
    run, sends, _naps = _run(
        mission, armed=["COM3"], classify=lambda cmd: "lab-only"
    )
    assert [r.status for r in run.results] == [StepStatus.SENT]
    assert sends.sent == [("COM3", "deauth 0")]


def test_arm_is_per_port_not_global():
    mission = _mission(
        MissionStep("COM3", "deauth 0"),
        MissionStep("COM7", "deauth 0"),
        devices=["COM3", "COM7"],
    )
    run, sends, _naps = _run(mission, armed=["COM3"], classify=lambda cmd: "lab-only")
    # Armed COM3 fires; un-armed COM7 is refused.
    assert sends.sent == [("COM3", "deauth 0")]
    assert run.refused[0].port == "COM7"


def test_default_classifier_is_real_safety_gate():
    # No classify injected -> the runner uses safety.classify. 'deauth 0' is lab-only for real.
    mission = _mission(
        MissionStep("COM3", "scan"),
        MissionStep("COM3", "deauth 0"),
    )
    sends = RecordingSender()
    runner = MissionRunner(sends, is_armed=lambda p: False, sleep=lambda _s: None)
    run = runner.run(plan_mission(mission))
    assert sends.sent == [("COM3", "scan")]  # real safety.classify refused the deauth
    assert run.refused[0].command == "deauth 0"


def test_fail_closed_when_no_armed_source():
    # Default is_armed refuses everything: an offensive verb never fires without an explicit ARM.
    mission = _mission(MissionStep("COM3", "jam"))  # illegal-tx for real
    sends = RecordingSender()
    run = MissionRunner(sends, sleep=lambda _s: None).run(plan_mission(mission))
    assert sends.sent == []
    assert run.refused[0].command == "jam"


# ── failure stops the chain ──────────────────────────────────────────

def test_send_failure_marks_failed_and_stops_chain():
    mission = _mission(
        MissionStep("COM3", "scan"),
        MissionStep("COM3", "boom"),
        MissionStep("COM3", "never"),
    )
    sends = RecordingSender(fail_on="boom")
    runner = MissionRunner(sends, classify=lambda cmd: "", sleep=lambda _s: None)
    run = runner.run(plan_mission(mission))
    assert [r.status for r in run.results] == [StepStatus.SENT, StepStatus.FAILED]
    assert sends.sent == [("COM3", "scan")]  # 'never' was never attempted
    assert not run.ok
    assert "boom" in run.failed[0].detail


# ── skipped (condition-gated) steps never send ───────────────────────

def test_condition_gated_step_is_not_sent():
    mission = _mission(
        MissionStep("COM3", "scan"),
        MissionStep("COM7", "list", condition=StepCondition.DEVICE_CONNECTED),
        devices=["COM3", "COM7"],
    )
    # COM7 is not connected -> that step is skipped by the planner -> runner never sees it.
    run, sends, _naps = _run(mission, context=MissionContext(connected_ports=frozenset({"COM3"})))
    assert sends.sent == [("COM3", "scan")]
    assert len(run.sent) == 1


# ── run_mission convenience + repeat expansion ───────────────────────

def test_run_mission_convenience_with_armed_ports():
    mission = _mission(MissionStep("COM3", "deauth 0"))
    sends = RecordingSender()
    run = run_mission(mission, send=sends, armed_ports=["COM3"], sleep=lambda _s: None)
    assert sends.sent == [("COM3", "deauth 0")]
    assert run.ok


def test_repeat_count_expands_sends():
    mission = _mission(MissionStep("COM3", "scan"))
    mission.repeat_count = 2  # 3 passes total
    sends = RecordingSender()
    run_mission(mission, send=sends, sleep=lambda _s: None)
    assert sends.sent == [("COM3", "scan")] * 3


# ── the real-send factory ────────────────────────────────────────────

class _FakeConn:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, data: str) -> None:
        self.writes.append(data)


class _FakeDM:
    def __init__(self, conns: dict[str, _FakeConn]) -> None:
        self._conns = conns

    def get_connection(self, port: str):
        return self._conns.get(port)


def test_device_manager_sender_writes_to_connection():
    conn = _FakeConn()
    send = device_manager_sender(_FakeDM({"COM3": conn}))
    send("COM3", "scan")
    assert conn.writes == ["scan"]


def test_device_manager_sender_raises_without_connection():
    send = device_manager_sender(_FakeDM({}))
    with pytest.raises(RuntimeError, match="No open connection"):
        send("COM3", "scan")


# ── dry-run rendering ────────────────────────────────────────────────

def test_format_plan_marks_refused_and_runnable():
    mission = _mission(
        MissionStep("COM3", "scan", delay_after=1.0),
        MissionStep("COM3", "deauth 0"),
    )
    out = format_plan(plan_mission(mission), armed_ports=[])
    assert "Mission: m" in out
    assert "scan" in out and "deauth 0" in out
    assert "REFUSED" in out  # real safety.classify flags deauth, COM3 not armed


def test_format_plan_reports_non_runnable():
    mission = Mission(name="bad", devices=["COM3"], steps=[MissionStep("COM9", "scan")])
    out = format_plan(plan_mission(mission))
    assert "NOT RUNNABLE" in out
