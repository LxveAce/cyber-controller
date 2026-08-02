"""QuickActionsStrip (WS3 step 4) - the one-tap action strip stays guarded + honest.

Builds a real strip driven by a live OperateTab (same fake-DM harness as the wrapper test): a no-arg
tile fires run_curated, an arg tile opens an inline OpPanel, STOP is two-mode + never gated,
readiness disables an arm-gated verb until armed, and an empty catalog yields the honest hint (only
STOP). Every send still routes through the guarded path; safety.py is the authority.
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


def _strip(tab, cis, supports_arm=False, stop_ci=None):
    from src.ui.qt.quick_actions_strip import QuickActionsStrip
    s = QuickActionsStrip()
    s.set_actions(cis, tab.run_curated, tab._send, tab.ready_for, tab.safe_state,
                  supports_arm, stop_ci)
    return s


def test_tiles_and_stop_are_built(qapp):
    from src.models.device import Device
    from src.protocols.base import CommandInfo
    tab = _tab(Device(port="COM23", firmware="marauder", connected=True), _FakeConn())
    cis = [CommandInfo("scanall", "WiFi"), CommandInfo("info", "Device")]
    s = _strip(tab, cis)
    assert [c.name for c, _ in s._tiles] == ["scanall", "info"]   # one tile per curated verb
    assert s._stop_btn is not None                                # STOP always present


def test_no_arg_tile_fires_through_the_guard(qapp):
    from src.models.device import Device
    from src.protocols.base import CommandInfo
    conn = _FakeConn()
    tab = _tab(Device(port="COM23", firmware="marauder", connected=True), conn)
    s = _strip(tab, [CommandInfo("scanall", "WiFi")])
    s._tiles[0][1].click()
    assert conn.writes == ["scanall"]                             # run_curated -> _send -> write


def test_arg_tile_opens_an_op_panel_not_an_immediate_write(qapp):
    from src.models.device import Device
    from src.protocols.base import CommandInfo
    conn = _FakeConn()
    tab = _tab(Device(port="COM23", firmware="marauder", connected=True), conn)
    s = _strip(tab, [CommandInfo("channel", "WiFi", args="ch")])   # arg verb
    s._tiles[0][1].click()
    assert s._open_panel is not None and conn.writes == []        # opens OpPanel, does not one-tap


def test_stop_disarms_on_arming_firmware(qapp):
    from src.models.device import Device
    from src.protocols.base import CommandInfo
    conn = _FakeConn()
    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "armed"
    tab = _tab(dev, conn)
    s = _strip(tab, [CommandInfo("scan", "Recon")], supports_arm=True)
    s._stop_btn.click()
    assert conn.writes == ["disarm"]                              # STOP -> safe_state -> disarm


def test_stop_is_disabled_when_no_stop_verb(qapp):
    from src.models.device import Device
    from src.protocols.base import CommandInfo
    tab = _tab(Device(port="COM23", firmware="marauder", connected=True), _FakeConn())
    s = _strip(tab, [CommandInfo("scanall", "WiFi")], supports_arm=False, stop_ci=None)
    assert not s._stop_btn.isEnabled()     # honest disabled chip, not a fake button


def test_readiness_gates_an_arm_verb_until_armed(qapp):
    from src.models.device import Device
    from src.protocols.base import CommandInfo
    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "safe"
    tab = _tab(dev, _FakeConn())
    s = _strip(tab, [CommandInfo("evilportal", "Offensive", danger="lab-only")], supports_arm=True)
    assert not s._tiles[0][1].isEnabled()     # dangerous + not armed -> disabled
    dev.arm_state = "armed"
    s.refresh_readiness()
    assert s._tiles[0][1].isEnabled()                             # armed -> tile enabled


def test_empty_catalog_shows_the_honest_hint(qapp):
    from src.models.device import Device
    tab = _tab(Device(port="COM23", firmware="marauder", connected=True), _FakeConn())
    s = _strip(tab, [])
    assert s._tiles == [] and s._stop_btn is not None             # only STOP
    assert s._hint.text() != ""                                   # honest "no one-tap actions" hint
