"""Mission runner — the thin, side-effecting layer that executes a resolved :class:`MissionPlan`.

:mod:`src.core.mission_planner` decides *what* runs and in what order but performs no I/O. This
runner is what actually sends: it walks a plan's :attr:`~MissionPlan.active_steps` in order,
dispatches each step's command to its device, and waits ``delay_after`` between them.

It is written to be unit-testable **without hardware**: the sender, the danger classifier, the
ARMED check, and the sleep are all injected. :func:`device_manager_sender` wires the real serial
send for production; :func:`run_mission` is the plan-then-run convenience.

Safety (never bypass :mod:`src.core.safety`):

* Every command is classified with the SAME :func:`safety.classify` the Operate console's send path
  uses. A command it marks ``lab-only`` / ``illegal-tx`` is an **offensive verb**.
* An offensive verb is **refused on a device that is not ARMED** — recorded as ``refused-unsafe``
  with no byte reaching the port. This mirrors the Operate console, whose offensive-TX buttons stay
  disabled until the device is ARMED.
* The ARMED source is injected and **fails closed**: with nothing wired, every port reads SAFE, so
  an offensive step is refused rather than silently fired.

The runner never authors a transmit frame and never downgrades a danger level; it only gates and
forwards command strings the mission already contains.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum

from src.core import safety
from src.core.mission_planner import (
    MissionContext,
    MissionPlan,
    PlannedStep,
    plan_mission,
)
from src.models.mission import Mission

log = logging.getLogger(__name__)

#: Sends one command string to one port. Returns nothing; raises on failure.
Sender = Callable[[str, str], None]
#: True when a port is ARMED (offensive verbs allowed on it).
ArmedCheck = Callable[[str], bool]
#: Danger classifier — same contract as ``safety.classify(cmd) -> "" | "lab-only" | "illegal-tx"``.
Classifier = Callable[[str], str]
#: Sleep for N seconds (injected so tests never actually wait).
Sleeper = Callable[[float], None]


class StepStatus(Enum):
    """Outcome of a single mission step at run time."""

    SENT = "sent"
    FAILED = "failed"
    REFUSED_UNSAFE = "refused-unsafe"


class MissionRefused(Exception):
    """Raised when a plan is not runnable (structural validation errors) — nothing is sent."""


@dataclass(frozen=True)
class StepResult:
    """What happened to one active step."""

    planned: PlannedStep
    status: StepStatus
    detail: str = ""

    @property
    def port(self) -> str:
        return self.planned.step.device_port

    @property
    def command(self) -> str:
        return self.planned.step.command


@dataclass(frozen=True)
class MissionRun:
    """The record of a mission execution — one :class:`StepResult` per attempted active step."""

    mission_name: str
    results: tuple[StepResult, ...] = field(default_factory=tuple)

    @property
    def sent(self) -> tuple[StepResult, ...]:
        return tuple(r for r in self.results if r.status is StepStatus.SENT)

    @property
    def refused(self) -> tuple[StepResult, ...]:
        return tuple(r for r in self.results if r.status is StepStatus.REFUSED_UNSAFE)

    @property
    def failed(self) -> tuple[StepResult, ...]:
        return tuple(r for r in self.results if r.status is StepStatus.FAILED)

    @property
    def ok(self) -> bool:
        """True when no step failed to send (refused-unsafe is a deliberate safety outcome, not a
        failure)."""
        return not self.failed


def _default_armed(_port: str) -> bool:
    """Fail closed: with no ARMED source wired, every device reads SAFE so an offensive verb is
    refused rather than silently fired."""
    return False


class MissionRunner:
    """Executes a :class:`MissionPlan`. All effects (send / arm-check / sleep) are injected."""

    def __init__(
        self,
        send: Sender,
        *,
        classify: Classifier | None = None,
        is_armed: ArmedCheck | None = None,
        sleep: Sleeper | None = None,
    ) -> None:
        self._send = send
        self._classify = classify if classify is not None else safety.classify
        self._is_armed = is_armed if is_armed is not None else _default_armed
        if sleep is not None:
            self._sleep = sleep
        else:
            import time

            self._sleep = time.sleep

    def run(self, plan: MissionPlan) -> MissionRun:
        """Execute *plan*'s active steps in order.

        Refuses a non-runnable plan up front (raises :class:`MissionRefused` — nothing is sent). For
        each active step: an offensive verb on a non-ARMED port is recorded ``refused-unsafe`` and
        skipped; otherwise the command is sent and ``delay_after`` is honoured. A send that raises
        is recorded ``failed`` and **stops the chain** (later steps commonly depend on it).
        """
        if not plan.runnable:
            raise MissionRefused(
                f"Mission '{plan.mission_name}' is not runnable: {'; '.join(plan.errors)}"
            )

        results: list[StepResult] = []
        for pstep in plan.active_steps:
            step = pstep.step
            port, command = step.device_port, step.command

            danger = self._classify(command)
            if danger and not self._is_armed(port):
                log.info("mission '%s': refusing %s on un-ARMED %s (%s)",
                         plan.mission_name, danger, port, command)
                results.append(
                    StepResult(
                        pstep,
                        StepStatus.REFUSED_UNSAFE,
                        f"'{command}' is {danger}; {port} is not ARMED",
                    )
                )
                continue

            try:
                self._send(port, command)
            except Exception as exc:  # noqa: BLE001 — surface any sender error as a failed step
                results.append(
                    StepResult(pstep, StepStatus.FAILED, f"{type(exc).__name__}: {exc}")
                )
                break  # a failed link breaks the chain — do not fire the rest

            results.append(StepResult(pstep, StepStatus.SENT))
            if step.delay_after > 0:
                self._sleep(step.delay_after)

        return MissionRun(mission_name=plan.mission_name, results=tuple(results))


def device_manager_sender(device_manager) -> Sender:
    """Build a :data:`Sender` that writes over the device manager's open serial connections.

    Raises ``RuntimeError`` for a port with no open connection so the runner records it as a failed
    step rather than silently dropping the command.
    """

    def _send(port: str, command: str) -> None:
        conn = device_manager.get_connection(port)
        if conn is None:
            raise RuntimeError(f"No open connection to {port}")
        conn.write(command)

    return _send


def run_mission(
    mission: Mission,
    *,
    send: Sender,
    context: MissionContext | None = None,
    armed_ports: Iterable[str] = (),
    classify: Classifier | None = None,
    sleep: Sleeper | None = None,
) -> MissionRun:
    """Plan *mission* against *context*, then run it — the one-call convenience.

    ``armed_ports`` is the explicit set of ARMED ports; any offensive verb on a port outside it is
    refused. Raises :class:`MissionRefused` if the mission does not validate.
    """
    plan = plan_mission(mission, context)
    armed = frozenset(armed_ports)
    runner = MissionRunner(
        send,
        classify=classify,
        is_armed=lambda p: p in armed,
        sleep=sleep,
    )
    return runner.run(plan)


def format_plan(plan: MissionPlan, *, armed_ports: Iterable[str] = ()) -> str:
    """Render a plan as a human-readable schedule (the CLI dry-run view) — pure, no I/O.

    Marks each active step, previews which offensive verbs would be refused for lack of ARM, lists
    skipped steps with their reason, and prints the lower-bound timeline.
    """
    armed = frozenset(armed_ports)
    lines = [f"Mission: {plan.mission_name}"]
    if not plan.runnable:
        lines.append("  NOT RUNNABLE:")
        lines += [f"    - {e}" for e in plan.errors]
        return "\n".join(lines)

    active = plan.active_steps
    lines.append(f"  {len(active)} active step(s), ~{plan.total_delay:g}s total delay:")
    for i, pstep in enumerate(active, 1):
        step = pstep.step
        danger = safety.classify(step.command)
        if danger and step.device_port not in armed:
            mark = f"REFUSED ({danger}, {step.device_port} not ARMED)"
        elif danger:
            mark = f"send [{danger}, ARMED]"
        else:
            mark = "send"
        delay = f" then wait {step.delay_after:g}s" if step.delay_after > 0 else ""
        lines.append(f"    {i}. {step.device_port} <- {step.command!r}  [{mark}]{delay}")

    skipped = [s for s in plan.steps if not s.will_run]
    if skipped:
        lines.append(f"  {len(skipped)} skipped:")
        for s in skipped:
            lines.append(f"    - {s.step.device_port} {s.step.command!r}: {s.skip_reason}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``py -m src.core.mission_runner MISSION.json`` — dry-runs by default.

    Loads a saved mission, plans it against the supplied context flags, and prints the schedule.
    ``--execute`` actually sends over the device manager's open connections; any offensive verb is
    still refused unless its port is passed with ``--arm`` (safety is never bypassed).
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="mission_runner", description="Plan and (optionally) run a Cyber Controller mission."
    )
    parser.add_argument("mission_file", help="Path to a mission JSON file (Mission.to_dict shape).")
    parser.add_argument("--connected", action="append", default=[], metavar="PORT",
                        help="Mark a port connected for condition evaluation (repeatable).")
    parser.add_argument("--target-found", action="store_true", help="Context: a target was found.")
    parser.add_argument("--handshake", action="store_true", help="Context: a handshake captured.")
    parser.add_argument("--arm", action="append", default=[], metavar="PORT",
                        help="Treat a port as ARMED so offensive verbs may run on it (repeatable).")
    parser.add_argument("--execute", action="store_true",
                        help="Actually send over open serial connections (default: dry-run only).")
    args = parser.parse_args(argv)

    with open(args.mission_file, encoding="utf-8") as fh:
        mission = Mission.from_dict(json.load(fh))

    context = MissionContext(
        connected_ports=frozenset(args.connected),
        targets_found=args.target_found,
        handshake_captured=args.handshake,
    )
    plan = plan_mission(mission, context)

    if not args.execute:
        print(format_plan(plan, armed_ports=args.arm))
        print("\n(dry-run - pass --execute to send over open connections)")
        return 0 if plan.runnable else 1

    from src.core.device_manager import DeviceManager

    dm = DeviceManager()
    try:
        run = run_mission(
            mission, send=device_manager_sender(dm), context=context, armed_ports=args.arm
        )
    finally:
        dm.shutdown()
    for r in run.results:
        print(f"  {r.status.value:14} {r.port} <- {r.command!r}"
              + (f"  ({r.detail})" if r.detail else ""))
    return 0 if run.ok else 1


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
