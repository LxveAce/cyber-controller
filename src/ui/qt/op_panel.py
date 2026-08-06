"""One operator operation, rendered as a :class:`biscuit.OperationDetail` wired to the EXISTING
guarded send (Spade P2). This is the canonical execution atom the design calls for — big Start/Stop,
a HelpSheet + ModeSegment derived from the firmware command, honest ``set_ready`` disabling — with
its Start routed through the SAME operate_tab._send path (safety.classify + tx_hard_block two-factor
arm + confirm). It never reimplements the safety floor, and never one-taps an offensive op.

Given a firmware ``CommandInfo`` (``ci``), a ``send(cmd, ci)`` callable (the guarded send), and an
optional readiness provider ``ready_fn() -> (ready: bool, reason: str)`` (the arm staircase), it:

* builds the OperationDetail's title / modes / help from :mod:`src.core.op_spec` (the pure seam),
* on Start, sends ``op_spec.op_command(ci, arg)`` through the guarded ``send`` — the send decides
  whether the write actually lands (an offensive verb is refused unless the device is armed),
* on ``refresh_ready()``, disables Start with the readiness reason until the op can run — so the UX
  gate and the safety gate agree, and the operator sees the next action ('arm to transmit', …).

This module is the tested building block; wiring it into Operate-Home (and deleting the old
domain-browser scaffold) is the next increment.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from PyQt5.QtWidgets import QVBoxLayout, QWidget

from src.core import op_spec
from src.ui.qt.biscuit import OperationDetail


class OpPanel(QWidget):
    """A :class:`biscuit.OperationDetail` bound to one firmware command + the guarded send.

    ``send`` is the operate-console guarded send (``operate_tab._send``-style ``(cmd, ci)``); it is
    REUSED verbatim, never reimplemented. ``ready_fn`` (optional) returns ``(ready, reason)`` — the
    arm-staircase state — and gates Start. ``stop_cmd`` (optional) is the verb sent on Stop; when
    unset, Stop is a no-op here (a one-shot op self-terminates).
    """

    def __init__(self, ci: Any, send: Callable[[str, Any], None], *,
                 ready_fn: "Optional[Callable[[], tuple]]" = None,
                 stop_cmd: "Optional[str]" = None,
                 arg: str = "",
                 parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._ci = ci
        self._send = send
        self._ready_fn = ready_fn
        self._stop_cmd = stop_cmd
        self._arg = arg or ""
        self._detail = OperationDetail(
            title=(getattr(ci, "description", "") or op_spec.pretty_label(ci) or "op"),
            modes=op_spec.op_modes(ci),
            help_spec=op_spec.op_help_spec(ci),
        )
        self._detail.start_requested.connect(self._on_start)
        self._detail.stop_requested.connect(self._on_stop)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._detail)
        self.refresh_ready()

    @property
    def detail(self) -> OperationDetail:
        return self._detail

    def set_arg(self, arg: str) -> None:
        """Set the argument string a 'Manual' op will send (from the ModeSegment / an arg field)."""
        self._arg = arg or ""

    def refresh_ready(self) -> None:
        """Recompute Start's enabled-ness from the readiness provider (the arm staircase). With no
        provider, the op is always ready (a safe op on a connected device)."""
        if self._ready_fn is None:
            self._detail.set_ready(True)
            return
        try:
            ready, reason = self._ready_fn()
        except Exception:   # a readiness probe must never break the panel
            ready, reason = True, ""
        self._detail.set_ready(bool(ready), reason or "")

    def set_running(self, running: bool, status: str = "") -> None:
        """Reflect the real op state (the host sets this once the op actually starts/stops)."""
        self._detail.set_running(running, status)

    def set_stats(self, stats: dict) -> None:
        self._detail.set_stats(stats)

    def _on_start(self) -> None:
        # Route the resolved command through the GUARDED send. The send (safety.classify +
        # tx_hard_block two-factor + confirm) is the authority on whether the write lands — we never
        # bypass it, so an offensive op is never a one-tap send here.
        self._send(op_spec.op_command(self._ci, self._arg), self._ci)

    def _on_stop(self) -> None:
        if self._stop_cmd:
            self._send(self._stop_cmd, self._ci)
