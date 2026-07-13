"""Regression: the persistent-terminal (bottom-left) Connect button must work without a manual
pre-tick (owner-reported 2026-07-13, REOPENS punch-list #1).

v1.7.1 (fc55693) fixed the *Devices-tab* left-column Connect/Disconnect, but the owner clicks the
*persistent-terminal* Connect/Disconnect at the app BOTTOM-LEFT. Those act on the checked device
list; with nothing ticked, Connect used to no-op (Disconnect already fell back to "all connected").
The fix gives Connect a symmetric fallback: with exactly one listed device it connects that one;
with several it stays explicit. Tests cover the pure resolution logic AND the real handler.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt.main_window import CyberControllerWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ── Pure resolution logic (no window needed) ─────────────────────────────────

def test_resolve_checked_wins():
    r = CyberControllerWindow._resolve_pterm_connect_ports(["COM4"], ["COM4", "COM7"])
    assert r == (["COM4"], None)


def test_resolve_sole_device_when_nothing_checked():
    # The owner's case: one device plugged in, Connect clicked without ticking -> connect it anyway.
    ports, msg = CyberControllerWindow._resolve_pterm_connect_ports([], ["COM4"])
    assert ports == ["COM4"]
    assert msg is None


def test_resolve_no_devices_gives_message():
    ports, msg = CyberControllerWindow._resolve_pterm_connect_ports([], [])
    assert ports == []
    assert msg and "No devices" in msg


def test_resolve_multiple_unchecked_stays_explicit():
    # Several devices, none ticked -> don't guess/open-all; ask the user to pick.
    ports, msg = CyberControllerWindow._resolve_pterm_connect_ports([], ["COM4", "COM7"])
    assert ports == []
    assert msg and "tick" in msg.lower()


# ── The REAL handler on a built window (the control the owner actually clicks) ─

def _build_window(qapp):
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine

    dm = DeviceManager()
    win = CyberControllerWindow(dm, FlashEngine(), EventBus(), TargetPool())
    return win, dm


def test_pterm_connect_opens_sole_device_with_no_tick(qapp, monkeypatch):
    """Drive the ACTUAL _pterm_on_connect handler: one device listed, nothing ticked -> it opens
    that device. The exact bottom-left Connect click the owner reported as dead."""
    from src.models.device import Device

    win, dm = _build_window(qapp)
    try:
        dm.add_device(Device(port="COM_TESTA", name="Marauder", firmware="marauder"))

        opened: list[str] = []

        class _FakeConn:
            is_connected = True

            def on_line(self, cb):  # the handler registers a line callback
                pass

        def _fake_open(port, baud=115200, owner=None):
            opened.append(port)
            return _FakeConn()

        monkeypatch.setattr(dm, "open_connection", _fake_open)

        win._pterm_refresh_ports()
        # Nothing is ticked by default (the exact precondition that used to no-op).
        assert win._pterm_checked_ports() == []

        win._pterm_on_connect()

        assert opened == ["COM_TESTA"], "bottom-left Connect must open the sole device with no tick"
        assert "COM_TESTA" in win._pterm_conns
    finally:
        win.close()


def test_pterm_connect_multiple_unticked_opens_nothing(qapp, monkeypatch):
    """Two devices, none ticked -> the handler opens nothing and stays explicit (no open-all)."""
    from src.models.device import Device

    win, dm = _build_window(qapp)
    try:
        dm.add_device(Device(port="COM_TESTB", name="Marauder", firmware="marauder"))
        dm.add_device(Device(port="COM_TESTC", name="Flipper", firmware="flipper"))

        opened: list[str] = []
        monkeypatch.setattr(dm, "open_connection", lambda port, **kw: opened.append(port))

        win._pterm_refresh_ports()
        win._pterm_on_connect()

        assert opened == [], "with several devices and none ticked, Connect must not open any"
    finally:
        win.close()


def test_pterm_disconnect_gives_feedback_when_nothing_connected(qapp):
    """The other half of the owner report: Disconnect used to run its loop zero times and print
    NOTHING when nothing was connected. It must always give feedback so the button never looks dead."""
    win, _dm = _build_window(qapp)
    try:
        assert win._pterm_conns == {}
        win._pterm_on_disconnect()
        assert "No connected devices to disconnect" in win._pterm_output.toPlainText()
    finally:
        win.close()
