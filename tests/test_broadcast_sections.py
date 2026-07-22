"""QA-1 Option B — the Broadcast/Console split. Offscreen.

Broadcast is now the PURE fan-out surface (one intent -> every connected device); the per-device
sections (force-firmware combo + per-firmware command grid) were removed and the force-firmware
control moved to the Console (OperateTab). These lock that invariant, plus the engine's still-valid
single-port plans (plan_for_port / plan_raw) that back a fan-out.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core.cross_comm import EventBus, TargetPool  # noqa: E402
from src.core.cross_comm_hub import CrossCommHub  # noqa: E402
from src.core.device_manager import DeviceManager  # noqa: E402
from src.models.device import Device  # noqa: E402
from src.ui.qt.broadcast_tab import BroadcastBar  # noqa: E402
from src.ui.qt.operate_tab import OperateTab  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _bar():
    dm = DeviceManager()
    bus = EventBus()
    hub = CrossCommHub(dm, bus, TargetPool(bus))
    return BroadcastBar(hub.broadcast, dm, bus), dm


# ── Broadcast is fan-out ONLY (no per-device sections) ────────────────────────
def test_broadcast_is_fanout_only_no_sections(qapp):
    bar, dm = _bar()
    # The per-device section machinery is gone (Option B): no sections dict, layout, or empty-hint.
    assert not hasattr(bar, "_sections")
    assert not hasattr(bar, "_sections_layout")
    assert not hasattr(bar, "_empty_hint")
    # The universal fan-out buttons + STOP ALL remain.
    assert bar._buttons, "fan-out verb buttons must still exist"
    assert bar._stop_btn is not None
    # Connecting a device just re-enables the fan-out; it does NOT spawn a section.
    dm.add_device(Device(port="COM7", firmware="marauder", connected=True))
    bar._on_timer()                    # the safety-net tick (was _rebuild_sections)
    assert not hasattr(bar, "_sections")


# ── the force-firmware control moved to the Console ───────────────────────────
def test_console_force_firmware_sets_and_clears_forced(qapp):
    dm = DeviceManager()
    dm.add_device(Device(port="COM7", firmware="marauder", connected=True))
    tab = OperateTab(dm)               # __init__ reloads devices + selects the only connected one
    assert tab._active_port == "COM7"

    # Force to the first real firmware in the combo (index 0 is "Clear forced firmware").
    tab._fw_combo.setCurrentIndex(1)   # fires _on_fw_changed -> dm.set_firmware(..., forced=True)
    forced_key = tab._fw_combo.itemData(1)
    dev = dm.get_device("COM7")
    assert dev.firmware == str(forced_key) and dev.firmware_forced is True

    # Selecting "Clear forced firmware" releases the force (keeps the firmware, no re-probe).
    tab._fw_combo.setCurrentIndex(0)
    assert dm.get_device("COM7").firmware_forced is False


def test_console_force_combo_first_item_is_honest_clear_label(qapp):
    dm = DeviceManager()
    dm.add_device(Device(port="COM7", firmware="marauder", connected=True))
    tab = OperateTab(dm)
    # index 0 = the honest "clear the force" label (not a fake "Auto-detect" that never probes).
    assert tab._fw_combo.itemText(0) == "Clear forced firmware"
    assert tab._fw_combo.itemData(0) is None


# ── engine single-port plans still back a fan-out (unchanged by Option B) ──────
def test_plan_for_port_targets_only_that_port(qapp):
    from src.core.broadcast import BroadcastVerb
    bar, dm = _bar()
    dm.add_device(Device(port="COM7", firmware="marauder", connected=True))
    dm.add_device(Device(port="COM8", firmware="marauder", connected=True))
    plan = bar._engine.plan_for_port("COM7", BroadcastVerb.FIND_APS)
    assert [c.port for c in plan.concrete] == ["COM7"]     # single device, not both


def test_plan_raw_single_port_classifies_and_skips_unknown(qapp):
    """plan_raw builds a one-command plan on the named port and reports an unknown port as skipped
    rather than dispatching a phantom send."""
    bar, dm = _bar()
    dm.add_device(Device(port="COM7", firmware="marauder", connected=True))
    plan = bar._engine.plan_raw("COM7", "scanap", label="Scan APs")
    assert [c.port for c in plan.concrete] == ["COM7"]
    assert plan.concrete[0].command == "scanap"
    assert plan.action.label == "Scan APs"

    skip = bar._engine.plan_raw("COM_NONE", "scanap")
    assert skip.concrete == [] and skip.skipped            # no device -> skipped, no phantom send
