"""OpPanel (src/ui/qt/op_panel.py) — the OperationDetail-backed op atom wired to the guarded send.

Tested in ISOLATION (Spade P2b): a fake send callable stands in for operate_tab._send, so this
asserts the wiring + the readiness/arm-staircase gate WITHOUT reimplementing safety. The real
safety floor (safety.classify + tx_hard_block two-factor + confirm) lives in the send this reuses.
Offscreen Qt.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core import op_spec  # noqa: E402
from src.ui.qt.op_panel import OpPanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _CI:
    def __init__(self, name, description="", args=""):
        self.name = name
        self.description = description
        self.args = args


def _recorder():
    sent = []
    return sent, (lambda cmd, ci: sent.append((cmd, ci)))


def test_start_routes_the_resolved_command_through_the_guarded_send(qapp):
    ci = _CI("scan")
    sent, send = _recorder()
    panel = OpPanel(ci, send)                      # no ready_fn -> ready
    panel._detail.start_requested.emit()           # the operator hits Start
    assert sent == [(op_spec.op_command(ci), ci)]  # routed verbatim through the (guarded) send


def test_manual_op_sends_the_arg(qapp):
    ci = _CI("deauth", args="-t <bssid>")
    sent, send = _recorder()
    panel = OpPanel(ci, send)
    panel.set_arg("-t AA:BB:CC:DD:EE:FF")
    panel._detail.start_requested.emit()
    assert sent == [("deauth -t AA:BB:CC:DD:EE:FF", ci)]


def test_readiness_gate_disables_start_with_an_honest_reason(qapp):
    # The arm staircase: until ready_fn reports ready, Start is disabled AND a disabled button can't
    # fire, so an offensive op is never one-tapped here (the send's two-factor is the second gate).
    state = {"ready": False, "reason": "SAFE — arm to transmit"}
    sent, send = _recorder()
    ready = lambda: (state["ready"], state["reason"])  # noqa: E731
    panel = OpPanel(_CI("deauth", args="-t x"), send, ready_fn=ready)
    assert not panel._detail._btn.isEnabled()      # not ready -> Start disabled
    panel._detail._btn.click()                      # a disabled button emits nothing
    assert sent == []                               # so nothing was sent

    state["ready"] = True                           # now armed/ready
    panel.refresh_ready()
    assert panel._detail._btn.isEnabled()
    panel._detail._btn.click()                      # an enabled click routes through the send
    assert len(sent) == 1


def test_help_and_modes_come_from_op_spec(qapp):
    sent, send = _recorder()
    assert OpPanel(_CI("scan"), send).detail.current_mode() == "Run"          # argless -> Run
    assert OpPanel(_CI("deauth", args="-t x"), send).detail.current_mode() == "Manual"


def test_stop_sends_the_stop_verb_only_when_configured(qapp):
    ci = _CI("sniff")
    sent, send = _recorder()
    panel = OpPanel(ci, send, stop_cmd="stop")
    panel.set_running(True)
    panel._detail.stop_requested.emit()
    assert sent == [("stop", ci)]

    sent2, send2 = _recorder()
    panel2 = OpPanel(_CI("scan"), send2)            # no stop_cmd -> Stop is a no-op
    panel2._detail.stop_requested.emit()
    assert sent2 == []
