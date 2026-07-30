"""D6 baseline (Spade P2, PRE-wiring): the OpPanel atom driven by the REAL OperateTab._send.

This proves the D6b injection contract BEFORE any production wiring — it composes the existing
OpPanel (p2b) with a live OperateTab (via the same fake-DM harness the console's own tests use) and
runs a real operator job end-to-end:

    Start -> op_spec.op_command(ci, arg) -> operate_tab._send -> safety.classify -> tx_hard_block
             -> (confirm) -> conn.write

It asserts the atom's ``send`` IS the console's guarded bound method (Atlas gate #1), that a real
job COMPLETES (gate #4), and that an offensive op CANNOT one-tap (gate #1). No production code
changes — this is the oracle the D6b OpPanel-in-console swap must keep green. safety.py is the
authority; nothing here reimplements it.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _FakeConn:
    """Captures writes — the only thing the guarded send touches at the wire."""

    def __init__(self) -> None:
        self.writes: list = []

    def write(self, data: str) -> None:
        self.writes.append(data)


class _FakeDM:
    """Minimal DeviceManager: one device + optional connection (mirrors test_operate_tab's fake)."""

    def __init__(self, dev, conn=None) -> None:
        self._dev = dev
        self._conn = conn

    def list_devices(self):
        return [self._dev]

    def get_device(self, port: str):
        return self._dev if self._dev.port == port else None

    def get_connection(self, port: str):
        return self._conn if (self._conn is not None and self._dev.port == port) else None


def _tab(dev, conn=None):
    from src.ui.qt.operate_tab import OperateTab

    tab = OperateTab(_FakeDM(dev, conn))
    tab._active_port = dev.port
    tab._grid_fw = ""          # force a grid (re)build for the device firmware
    tab._refresh()
    return tab


def _panel(ci, tab, **kw):
    # The atom is handed the console's REAL guarded send (a bound method) — verbatim, never a shim.
    from src.ui.qt.op_panel import OpPanel

    return OpPanel(ci, tab._send, **kw)


def test_oppanel_send_is_the_real_operate_tab_send(qapp):
    # Gate #1 (the injection contract): the atom's send IS operate_tab._send, bound to THIS console
    # instance — never a conn.write shortcut, never a re-derived send.
    from src.models.device import Device
    from src.protocols.base import CommandInfo

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    tab = _tab(dev, _FakeConn())
    panel = _panel(CommandInfo("scan", "Recon", "Passive Wi-Fi AP scan"), tab)
    assert panel._send == tab._send            # same bound method
    assert panel._send.__self__ is tab         # bound to the real device-holding console


def test_oppanel_completes_a_safe_op_through_the_guarded_send(qapp):
    # Gate #4: a real SAFE job completes end-to-end — Start -> op_command -> _send -> conn.write.
    from src.models.device import Device
    from src.protocols.base import CommandInfo

    conn = _FakeConn()
    dev = Device(port="COM23", firmware="lxveos", connected=True)
    tab = _tab(dev, conn)
    panel = _panel(CommandInfo("scan", "Recon", "Passive Wi-Fi AP scan"), tab)
    panel.detail.start_requested.emit()        # the operator taps Start
    assert conn.writes == ["scan"]             # reached the wire through the full guard chain


def test_oppanel_cannot_one_tap_an_offensive_op(qapp, monkeypatch):
    # Gate #1 (no one-tap): an offensive-TX verb with the device NOT armed writes NOTHING — the real
    # _send's tx_hard_block refuses even though Start (no ready_fn) was enabled. Arm -> the same
    # Start lands, proving the safety floor out-ranks the atom's UX. safety.py is the authority.
    from src.models.device import Device
    from src.protocols.base import CommandInfo
    from src.ui.qt import operate_tab

    conn = _FakeConn()
    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "safe"
    tab = _tab(dev, conn)
    ci = CommandInfo("evilportal", "Offensive", "rogue AP + captive portal", danger="lab-only")
    panel = _panel(ci, tab, arg="evilportal karma")

    panel.detail.start_requested.emit()        # tap Start while SAFE
    assert conn.writes == []                    # refused by tx_hard_block — never a one-tap TX

    # Arm the device + silence the confirm dialog; the SAME Start now completes.
    monkeypatch.setattr(operate_tab.safety, "should_confirm", lambda *a, **k: False)
    dev.arm_state = "armed"
    panel.detail.start_requested.emit()
    assert conn.writes == ["evilportal karma"]


def test_oppanel_ready_fn_gates_start_when_not_armed(qapp):
    # The UX layer that pairs with the floor: given a ready_fn for the arm staircase, Start is
    # disabled with an honest reason until armed (so the operator sees 'arm to transmit', not a
    # silently-refused tap). This is the shape D6b's real _op_ready_fn will provide.
    from src.models.device import Device
    from src.protocols.base import CommandInfo

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "safe"
    tab = _tab(dev, _FakeConn())
    ci = CommandInfo("evilportal", "Offensive", "rogue AP + captive portal", danger="lab-only")

    def ready():
        armed = getattr(tab._active_device(), "arm_state", "") == "armed"
        return (armed, "" if armed else "arm to transmit")

    panel = _panel(ci, tab, ready_fn=ready)
    assert not panel.detail._btn.isEnabled()     # SAFE -> Start disabled with the arm reason
    dev.arm_state = "armed"
    panel.refresh_ready()
    assert panel.detail._btn.isEnabled()         # ARMED -> Start available
