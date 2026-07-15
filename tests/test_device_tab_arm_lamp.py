"""Devices-tab ARM/SAFE lamp — a firmware that reports its offensive-TX arm state over serial
(LxveOS ``arm``/``disarm`` -> ``arm_state`` events, or a ``status`` line's ``tx=`` field) drives a
prominent color-coded lamp: green SAFE / amber PENDING / red ARMED / grey TX-DISABLED. Blank until
the firmware speaks. Offscreen Qt.

Builds on the arm-state data path (Device.apply_arm_state / ingestor arm_state route); this is the
Devices-tab surface that makes the live arm state visible where a board is connected.
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


# ── pure render helper (no widget needed) ────────────────────────────

def test_arm_lamp_render_known_states():
    from src.ui.qt.device_tab import DeviceTab

    for state, needle, color in (
        ("safe", "SAFE", "#3fb950"),
        ("pending", "PENDING", "#d29922"),
        ("armed", "ARMED", "#f85149"),
        ("tx_disabled", "TX DISABLED", "#6e7681"),
    ):
        text, col = DeviceTab._arm_lamp_render(state)
        assert needle in text and col == color
    # "armed" and "safe" must not collide (ARMED text must not merely be a substring match on SAFE)
    assert DeviceTab._arm_lamp_render("armed")[0] != DeviceTab._arm_lamp_render("safe")[0]


def test_arm_lamp_render_blank_is_blank_and_unknown_is_verbatim():
    from src.ui.qt.device_tab import DeviceTab

    assert DeviceTab._arm_lamp_render("") == ("", "#8b949e")
    # a future/unknown token is shown verbatim (muted) rather than dropped
    text, col = DeviceTab._arm_lamp_render("recovering")
    assert "recovering" in text and col == "#8b949e"


# ── live surface on the tab ──────────────────────────────────────────

def _tab_with_device(dev):
    from src.core.device_manager import DeviceManager
    from src.ui.qt.device_tab import DeviceTab

    dm = DeviceManager()
    dm.add_device(dev)
    tab = DeviceTab(dm)
    tab._active_port = dev.port
    return tab


def test_arm_lamp_reflects_device_arm_state(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "armed"
    tab = _tab_with_device(dev)
    tab._update_arm_lamp()
    assert "ARMED" in tab._arm_label.text()
    assert "#f85149" in tab._arm_label.styleSheet()  # red = hot


def test_arm_lamp_falls_back_to_tx_telemetry_when_no_arm_event(qapp):
    # No explicit arm event yet, but a `status` line carried tx= -> the lamp derives a coarse state.
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.telemetry = {"tx": True}
    tab = _tab_with_device(dev)
    tab._update_arm_lamp()
    assert "ARMED" in tab._arm_label.text()

    dev.arm_state = ""
    dev.telemetry = {"tx": False}
    tab._last_arm_state = None  # force a re-render for the test
    tab._update_arm_lamp()
    assert "SAFE" in tab._arm_label.text()


def test_explicit_arm_state_wins_over_tx_telemetry(qapp):
    # An authoritative arm_state event must take precedence over the coarse tx= fallback.
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "pending"
    dev.telemetry = {"tx": False}  # stale/coarse; the explicit pending must win
    tab = _tab_with_device(dev)
    tab._update_arm_lamp()
    assert "PENDING" in tab._arm_label.text()


def test_incoming_arm_line_refreshes_lamp(qapp):
    # The live-refresh wire: the ingestor updates Device.arm_state on the serial thread; then
    # _on_line_received runs on the Qt thread and must repaint the lamp from the (updated) Device.
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    tab = _tab_with_device(dev)
    tab._update_arm_lamp()
    assert tab._arm_label.text() == ""  # nothing reported yet

    dev.arm_state = "armed"  # ingestor applied an `arm state=armed` line
    tab._on_line_received("COM23", "LXVEOS/1 arm state=armed")
    assert "ARMED" in tab._arm_label.text()


def test_arm_line_from_other_port_does_not_repaint_active(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "safe"
    tab = _tab_with_device(dev)
    tab._update_arm_lamp()
    before = tab._arm_label.text()
    dev.arm_state = "armed"  # mutate COM23, but a DIFFERENT port speaks
    tab._on_line_received("COM99", "noise from another device")
    assert tab._arm_label.text() == before  # unchanged — refresh is gated on the active port
