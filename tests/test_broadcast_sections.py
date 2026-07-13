"""Per-device Broadcast sections (Task 4) — reactive population + force-any-firmware. Offscreen."""
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


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _bar():
    dm = DeviceManager()
    bus = EventBus()
    hub = CrossCommHub(dm, bus, TargetPool(bus))
    return BroadcastBar(hub.broadcast, dm, bus), dm


def test_section_appears_and_disappears_with_device(qapp):
    bar, dm = _bar()
    assert bar._sections == {}
    dm.add_device(Device(port="COM7", firmware="marauder", connected=True))
    bar._rebuild_sections()
    assert "COM7" in bar._sections
    assert not bar._empty_hint.isVisible()
    dm.get_device("COM7").connected = False
    bar._rebuild_sections()
    assert "COM7" not in bar._sections


def test_force_firmware_updates_section_and_flag(qapp):
    bar, dm = _bar()
    dm.add_device(Device(port="COM7", firmware="marauder", connected=True))
    bar._rebuild_sections()
    # force to a different firmware — its command set becomes available on that device
    dm.set_firmware("COM7", "ghost-esp", forced=True)
    bar._rebuild_sections()
    dev = dm.get_device("COM7")
    assert dev.firmware == "ghost-esp" and dev.firmware_forced is True
    # ghost-esp's OWN per-firmware commands now drive the section buttons (§OP), not generic verbs.
    from src.core.quick_commands import grouped_quick_commands
    assert grouped_quick_commands("ghost-esp")


def test_per_device_launch_plans_only_that_port(qapp):
    from src.core.broadcast import BroadcastVerb
    bar, dm = _bar()
    dm.add_device(Device(port="COM7", firmware="marauder", connected=True))
    dm.add_device(Device(port="COM8", firmware="marauder", connected=True))
    plan = bar._engine.plan_for_port("COM7", BroadcastVerb.FIND_APS)
    assert [c.port for c in plan.concrete] == ["COM7"]  # single-device, not both


# ── §OP: per-firmware personalized Operate buttons (owner feature #3) ──────────────

def _grid_buttons(section):
    from PyQt5.QtWidgets import QPushButton
    grid = section._btn_grid
    return [
        grid.itemAt(i).widget()
        for i in range(grid.count())
        if isinstance(grid.itemAt(i).widget(), QPushButton)
    ]


def test_section_renders_firmwares_own_commands_not_generic_verbs(qapp):
    """§OP: a device section now shows its firmware's OWN one-tap commands (from
    grouped_quick_commands), and a different firmware renders a DIFFERENT set — not shared verbs."""
    from src.core.quick_commands import grouped_quick_commands
    bar, dm = _bar()
    dm.add_device(Device(port="COM7", firmware="marauder", connected=True))
    bar._rebuild_sections()
    labels = [b.text() for b in _grid_buttons(bar._sections["COM7"])]
    mar = {qc.label for _c, cmds in grouped_quick_commands("marauder") for qc in cmds}
    assert labels, "marauder section should render its own command buttons"
    assert set(labels) <= mar               # every button is a real marauder command label
    assert "Find APs" not in labels         # the generic universal-verb label is gone here

    dm.set_firmware("COM7", "ghost-esp", forced=True)
    bar._rebuild_sections()
    ghost_labels = [b.text() for b in _grid_buttons(bar._sections["COM7"])]
    assert ghost_labels and ghost_labels != labels  # personalized per firmware


def test_section_button_click_routes_raw_command_to_that_port(qapp, monkeypatch):
    """Clicking a per-firmware button dispatches that one raw command on THIS port through the
    normal launch path (plan_raw -> _launch). Drives the REAL button (verify-never-fake)."""
    bar, dm = _bar()
    dm.add_device(Device(port="COM7", firmware="marauder", connected=True))
    bar._rebuild_sections()
    captured = []
    monkeypatch.setattr(bar, "_launch", lambda plan: captured.append(plan))

    _grid_buttons(bar._sections["COM7"])[0].click()   # click a real rendered button

    assert len(captured) == 1
    plan = captured[0]
    assert [c.port for c in plan.concrete] == ["COM7"]         # only this device
    assert plan.concrete[0].command                            # a real (non-empty) firmware command
    assert plan.action.verb.value == "custom"                 # routed as a raw command, not a verb


def test_plan_raw_single_port_classifies_and_skips_unknown(qapp):
    """plan_raw builds a one-command plan on the named port, classifies its danger via safety (so
    the confirm gate can fire), and reports an unknown port as skipped rather than dispatching."""
    bar, dm = _bar()
    dm.add_device(Device(port="COM7", firmware="marauder", connected=True))
    plan = bar._engine.plan_raw("COM7", "scanap", label="Scan APs")
    assert [c.port for c in plan.concrete] == ["COM7"]
    assert plan.concrete[0].command == "scanap"
    assert plan.action.label == "Scan APs"

    skip = bar._engine.plan_raw("COM_NONE", "scanap")
    assert skip.concrete == [] and skip.skipped  # no device -> skipped, never a phantom send
