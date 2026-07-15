"""Operate console (B16) — a button-driven single-device console: status-poll header, SAFE/ARMED
lamp, two-factor arm toggle, and a per-firmware TX-gated command grid. It is a poll-driven,
read-only view of shared Device state and writes through the Devices tab's guarded path. Offscreen.

These cover the console's invariants: the shared arm-lamp render, the TX-lockout (offensive-TX
buttons enabled only when ARMED), the arm-token send path, and DMS/disconnect gating of the poll.
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
    """Captures writes; that's all the console's send/poll path touches."""

    def __init__(self) -> None:
        self.writes: list = []

    def write(self, data: str) -> None:
        self.writes.append(data)


class _FakeDM:
    """Minimal DeviceManager: one device + optional connection (list/get_device/get_connection)."""

    def __init__(self, dev, conn=None) -> None:
        self._dev = dev
        self._conn = conn

    def list_devices(self):
        return [self._dev]

    def get_device(self, port: str):
        return self._dev if self._dev.port == port else None

    def get_connection(self, port: str):
        return self._conn if (self._conn is not None and self._dev.port == port) else None


def _tab(dev, conn=None, dms_seen=None):
    from src.ui.qt.operate_tab import OperateTab

    tab = OperateTab(_FakeDM(dev, conn), dms_seen=dms_seen)
    tab._active_port = dev.port
    tab._grid_fw = ""          # force a grid (re)build for the device firmware on the next refresh
    tab._refresh()
    return tab


# ── shared render helper (no widget needed) ──────────────────────────

def test_console_uses_the_shared_arm_lamp_render():
    # The console and the Devices tab must render an identical lamp — same shared table.
    from src.ui.qt.arm_lamp import arm_lamp_render
    from src.ui.qt.device_tab import DeviceTab

    for state in ("safe", "pending", "armed", "tx_disabled", "", "future_state"):
        assert arm_lamp_render(state) == DeviceTab._arm_lamp_render(state)


# ── TX lockout: offensive-TX buttons only when ARMED ─────────────────

def test_tx_buttons_disabled_until_armed(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "safe"
    tab = _tab(dev)
    assert tab._tx_buttons, "lxveos catalog must contribute at least one offensive-TX button"
    assert all(not b.isEnabled() for b in tab._tx_buttons)   # SAFE -> TX locked
    assert all(b.isEnabled() for b in tab._safe_buttons)     # passive verbs available if connected


def test_tx_buttons_enable_when_armed(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "safe"
    tab = _tab(dev)
    assert all(not b.isEnabled() for b in tab._tx_buttons)

    dev.arm_state = "armed"                       # ingestor applied an `arm state=armed` line
    tab.on_line_received("COM23", "LXVEOS/1 arm state=armed")
    assert all(b.isEnabled() for b in tab._tx_buttons)   # ARMED -> offensive TX permitted
    assert "ARMED" in tab._arm_label.text()


def test_tx_buttons_stay_locked_when_disconnected(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=False)
    dev.arm_state = "armed"  # even a stale ARMED must not enable TX on a disconnected device
    tab = _tab(dev)
    assert all(not b.isEnabled() for b in tab._tx_buttons)
    assert all(not b.isEnabled() for b in tab._safe_buttons)


# ── arm-token send path ──────────────────────────────────────────────

def test_confirm_token_sends_arm_with_token(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "pending"
    conn = _FakeConn()
    tab = _tab(dev, conn)
    tab._token_edit.setText("428913")
    tab._on_confirm_token()
    assert conn.writes == ["arm 428913"]


def test_arm_and_disarm_buttons_send_bare_verbs(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    conn = _FakeConn()
    tab = _tab(dev, conn)
    tab._send("arm")
    tab._send("disarm")
    assert conn.writes == ["arm", "disarm"]


def test_send_without_connection_is_a_no_op(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    tab = _tab(dev, conn=None)  # no connection registered
    tab._send("arm")            # must not raise, just logs
    assert "no connection" in tab._log.toPlainText()


# ── auto-poll gating ─────────────────────────────────────────────────

def test_poll_sends_status_for_a_connected_lxveos_port(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    conn = _FakeConn()
    tab = _tab(dev, conn)
    tab._poll_tick()
    assert "status" in conn.writes  # lxveos defines a poll-safe `status` verb


def test_poll_skips_dms_gated_port(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    conn = _FakeConn()
    tab = _tab(dev, conn, dms_seen={"COM23"})  # a Dead-Man's-Switch prompt was seen here
    tab._poll_tick()
    assert conn.writes == []  # never auto-write to a DMS-gated port (could trip a wipe)


def test_poll_skips_disconnected_port(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=False)
    conn = _FakeConn()
    tab = _tab(dev, conn)
    tab._poll_tick()
    assert conn.writes == []


def test_poll_skips_firmware_without_a_status_verb(qapp):
    # A firmware whose catalog has no `status` must never get a stray auto-`status` write.
    from src.models.device import Device

    dev = Device(port="COM23", firmware="marauder", connected=True)
    conn = _FakeConn()
    tab = _tab(dev, conn)
    tab._poll_tick()
    assert "status" not in conn.writes
