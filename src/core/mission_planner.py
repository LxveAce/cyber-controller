"""Pure attack-chain planner over the :mod:`src.models.mission` model.

A :class:`~src.models.mission.Mission` is a reusable, condition-gated sequence of per-device
commands. This planner resolves a Mission plus a runtime :class:`MissionContext` (what is connected,
what has been found) into an ordered :class:`MissionPlan` — the flat execution schedule, with every
step marked ``will_run`` or carrying a ``skip_reason``. Like :class:`broadcast.BroadcastPlan`
it has **no side effects and no serial I/O**: it decides *what* would run and in what order; a thin
runner (later / elsewhere) actually sends the commands.

Condition semantics (evaluated against the context, statically):

* ``ALWAYS`` — always runs.
* ``DEVICE_CONNECTED`` — runs only if the step's port (or ``condition_args["port"]``) is connected.
* ``TARGET_FOUND`` — runs only if the context reports a target was found.
* ``HANDSHAKE_CAPTURED`` — runs only if the context reports a captured handshake.
* ``PREVIOUS_SUCCESS`` — a *structural* gate: runs only if some earlier step will run.
  Whether that earlier step actually *succeeds* is a runtime fact the planner cannot know, so the
  runner re-checks it at execution time; the plan reflects the best static view (skip when there is
  provably no prior active step to succeed).

Skipped steps are surfaced with a reason (never silently dropped) and contribute no delay to the
timeline, because a step that does not execute cannot wait afterward.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.models.mission import Mission, MissionStep, StepCondition


@dataclass(frozen=True)
class MissionContext:
    """A snapshot of the runtime facts the step conditions are evaluated against.

    All fields default to the empty/false state so an unparameterised plan shows exactly which steps
    are gated waiting on the world (connected devices, a found target, a captured handshake).
    """

    connected_ports: frozenset[str] = frozenset()
    targets_found: bool = False
    handshake_captured: bool = False


@dataclass(frozen=True)
class PlannedStep:
    """One entry in the expanded execution schedule."""

    seq: int             # 0-based position in the full expanded schedule
    iteration: int       # which repeat pass this belongs to (0-based)
    step_index: int      # index of the step within the mission's step list
    step: MissionStep
    will_run: bool
    skip_reason: str     # "" when will_run is True
    start_offset: float  # cumulative seconds of prior running steps' delays before this step


@dataclass(frozen=True)
class MissionPlan:
    """The resolved, ordered plan for a mission BEFORE anything is sent."""

    mission_name: str
    steps: tuple[PlannedStep, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def runnable(self) -> bool:
        """True when the mission validated cleanly (no structural errors)."""
        return not self.errors

    @property
    def active_steps(self) -> tuple[PlannedStep, ...]:
        """Only the steps that will actually execute, in order."""
        return tuple(s for s in self.steps if s.will_run)

    @property
    def total_delay(self) -> float:
        """Sum of ``delay_after`` over running steps — a lower-bound timeline estimate."""
        return sum(s.step.delay_after for s in self.active_steps)


def _condition_met(step: MissionStep, ctx: MissionContext, prior_active: bool) -> tuple[bool, str]:
    """Evaluate a step's gate. Returns (met, reason_when_not_met)."""
    cond = step.condition
    if cond is StepCondition.ALWAYS:
        return True, ""
    if cond is StepCondition.DEVICE_CONNECTED:
        port = step.condition_args.get("port", step.device_port)
        if port in ctx.connected_ports:
            return True, ""
        return False, f"device '{port}' not connected"
    if cond is StepCondition.TARGET_FOUND:
        return (True, "") if ctx.targets_found else (False, "no target found yet")
    if cond is StepCondition.HANDSHAKE_CAPTURED:
        return (True, "") if ctx.handshake_captured else (False, "no handshake captured yet")
    if cond is StepCondition.PREVIOUS_SUCCESS:
        if prior_active:
            return True, ""
        return False, "no prior step will run to succeed"
    # Unknown condition: fail closed with a clear reason rather than silently running.
    return False, f"unknown condition '{cond}'"


def plan_mission(mission: Mission, context: MissionContext | None = None) -> MissionPlan:
    """Resolve *mission* against *context* into an ordered :class:`MissionPlan` (no side effects).

    The step list is expanded ``mission.repeat_count + 1`` times (``repeat_count == 0`` means one
    pass, per the model). Each expanded step is gated by its condition; skipped steps carry a reason
    and add no delay. If the mission fails :meth:`Mission.validate`, the returned plan is marked
    non-``runnable`` and carries those errors (the schedule is still produced for inspection).
    """
    ctx = context if context is not None else MissionContext()
    errors = tuple(mission.validate())

    planned: list[PlannedStep] = []
    seq = 0
    offset = 0.0
    any_active = False  # whether any earlier step in the whole schedule will run
    iterations = max(1, mission.repeat_count + 1)

    for iteration in range(iterations):
        for step_index, step in enumerate(mission.steps):
            met, reason = _condition_met(step, ctx, prior_active=any_active)
            planned.append(
                PlannedStep(
                    seq=seq,
                    iteration=iteration,
                    step_index=step_index,
                    step=step,
                    will_run=met,
                    skip_reason="" if met else reason,
                    start_offset=offset,
                )
            )
            if met:
                any_active = True
                offset += step.delay_after
            seq += 1

    return MissionPlan(mission_name=mission.name, steps=tuple(planned), errors=errors)
