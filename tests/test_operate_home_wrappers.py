"""WS3 step 3 — OperateTab's Operate-Home delegation wrappers stay a GUARDED door.

`run_curated`/`ready_for`/`safe_state` are thin delegations so Home never reimplements the send
path. This pins they route through the REAL `_send` (classify -> tx_hard_block -> confirm -> write)
- which matters now the curated home surfaces attack/jam/spam verbs (owner opt-in). Same fake-DM
harness as `test_op_panel_operate_workflow`. safety.py is the authority.
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
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)


class _FakeDM:
    def __init__(self, dev, conn=None):
        self._dev, self._conn = dev, conn

    def list_devices(self):
        return [self._dev]

    def get_device(self, port):
        return self._dev if self._dev.port == port else None

    def get_connection(self, port):
        return self._conn if (self._conn is not None and self._dev.port == port) else None


def _tab(dev, conn=None):
    from src.ui.qt.operate_tab import OperateTab
    tab = OperateTab(_FakeDM(dev, conn))
    tab._active_port = dev.port
    tab._grid_fw = ""
    tab._refresh()
    return tab


def test_run_curated_fires_a_safe_verb_through_the_guard(qapp):
    from src.models.device import Device
    from src.protocols.base import CommandInfo
    conn = _FakeConn()
    tab = _tab(Device(port="COM23", firmware="lxveos", connected=True), conn)
    tab.run_curated(CommandInfo("scan", "Recon", "scan"))
    assert conn.writes == ["scan"]              # op_command -> _send -> conn.write


def test_run_curated_builds_the_arg_string(qapp):
    from src.models.device import Device
    from src.protocols.base import CommandInfo
    conn = _FakeConn()
    tab = _tab(Device(port="COM23", firmware="marauder", connected=True), conn)
    tab.run_curated(CommandInfo("channel", "WiFi"), arg="6")
    assert conn.writes == ["channel 6"]         # op_command joins verb + arg (no placeholder)


def test_run_curated_dangerous_verb_still_hits_tx_hard_block(qapp, monkeypatch):
    # THE guard regression: a dangerous verb, not-armed arming firmware -> NOTHING; armed -> the SAME
    # verb lands. Home's one-tap never bypasses the safety floor, even for offensive verbs.
    from src.models.device import Device
    from src.protocols.base import CommandInfo
    from src.ui.qt import operate_tab
    conn = _FakeConn()
    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "safe"
    tab = _tab(dev, conn)
    ci = CommandInfo("evilportal", "Offensive", "rogue AP", danger="lab-only")
    tab.run_curated(ci)
    assert conn.writes == []                     # tx_hard_block refused — no one-tap TX
    monkeypatch.setattr(operate_tab.safety, "should_confirm", lambda *a, **k: False)
    dev.arm_state = "armed"
    tab.run_curated(ci)
    assert conn.writes == ["evilportal"]         # armed -> the SAME verb lands


def test_ready_for_returns_a_callable_tuple(qapp):
    from src.models.device import Device
    from src.protocols.base import CommandInfo
    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "safe"
    tab = _tab(dev, _FakeConn())
    ready = tab.ready_for(CommandInfo("evilportal", "Offensive", danger="lab-only"))
    assert callable(ready)
    ok, reason = ready()
    assert ok is False and reason == "arm to transmit"       # dangerous + not armed
    assert tab.ready_for(CommandInfo("scan", "Recon"))()[0] is True   # safe + connected -> ready


def test_ready_for_disconnected(qapp):
    from src.models.device import Device
    from src.protocols.base import CommandInfo
    tab = _tab(Device(port="COM23", firmware="lxveos", connected=False), None)
    ok, reason = tab.ready_for(CommandInfo("scan", "Recon"))()
    assert ok is False and "connect" in reason


def test_safe_state_disarms_through_the_guard(qapp):
    from src.models.device import Device
    conn = _FakeConn()
    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "armed"
    tab = _tab(dev, conn)
    tab.safe_state()
    assert conn.writes == ["disarm"]             # STOP -> disarm via the guarded door
