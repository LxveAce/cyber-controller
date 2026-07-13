"""UI-audit Batch UI-4 (stale-state / lifecycle / feedback) regressions.

* flock_heatmap_tab (DATA LOSS): every live scan checkpointed to a FIXED filename, so a 2nd drive
  os.replace-overwrote the 1st drive's saved cameras. Now each scan gets a unique per-scan file.
* device_tab: the global firmware combo was never re-synced to the selected device, so with an explicit
  firmware chosen, clicking another device judged the WRONG one (BlueJammer panel + Send-disable on a
  non-jammer). Now _on_device_selected re-points the combo at the selected device's firmware.
* network_tab: graph-node sends swallowed every failure (`except: pass`) and never published
  action.executed, so a failed manual Deauth/Beacon/Karma was invisible. Now both success and failure
  publish + hit the status bar.
* settings_tab: showEvent reloaded from disk on every show, silently discarding unsaved edits. Now it
  skips the reload while the tab is dirty.
"""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ── flock: a new scan never overwrites a prior drive's checkpoint ─────────────────────────────────

def test_flock_new_checkpoint_path_is_unique_per_scan(qapp, tmp_path, monkeypatch):
    from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab

    tab = FlockHeatmapTab()
    monkeypatch.setattr(tab, "_flock_data_dir", lambda: str(tmp_path))

    p1 = tab._new_checkpoint_path()
    Path(p1).write_text("{}", encoding="utf-8")  # the first drive's file now exists on disk
    p2 = tab._new_checkpoint_path()

    assert p1 != p2  # a 2nd scan can never reuse (os.replace over) the 1st drive's file
    assert Path(p1).name.startswith("live-drive-")
    # the old fixed name still exists for the Load-dialog default / existing callers
    assert Path(tab._default_checkpoint_path()).name == "live-drive.geojson"


# ── device_tab: firmware combo follows the selected device ────────────────────────────────────────

def test_firmware_combo_resyncs_to_selected_device(qapp):
    from types import SimpleNamespace

    from src.core.device_manager import DeviceManager
    from src.ui.qt.device_tab import DeviceTab

    tab = DeviceTab(DeviceManager())

    # Explicitly pick BlueJammer (as if for device A).
    for i in range(tab._firmware_combo.count()):
        if "jammer" in tab._firmware_combo.itemText(i).lower():
            tab._firmware_combo.setCurrentIndex(i)
            break
    assert "jammer" in tab._firmware_combo.currentText().lower()

    # Selecting device B (an auto-detected Marauder, NOT force-picked) must NOT keep A's BlueJammer pick —
    # a non-forced device falls back to Auto-detect so re-autodetect still works and B isn't judged a jammer.
    tab._sync_firmware_combo_to(SimpleNamespace(firmware="marauder", firmware_forced=False))
    assert tab._firmware_combo.currentText() == "Auto-detect"
    tab._update_bj_panel()
    assert tab._bj_panel.isHidden()  # BlueJammer panel is gone for B

    # A device whose firmware was explicitly FORCED pins the combo to that firmware.
    tab._sync_firmware_combo_to(SimpleNamespace(firmware="marauder", firmware_forced=True))
    assert "marauder" in tab._firmware_combo.currentText().lower()


# ── network_tab: manual graph-node sends surface success AND failure ──────────────────────────────

def _network_tab(send_cmd, bus):
    from src.core.cross_comm import TargetPool
    from src.core.device_manager import DeviceManager
    from src.ui.qt.network_tab import NetworkTab

    return NetworkTab(DeviceManager(), TargetPool(), None, send_cmd, bus)


def test_network_device_cmd_publishes_failure(qapp, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox

    from src.core.cross_comm import EventBus

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Yes))
    bus = EventBus()
    events: list = []
    bus.subscribe("action.executed", lambda topic, payload: events.append(payload))

    def _boom(port, cmd):
        raise RuntimeError("port busy")

    tab = _network_tab(_boom, bus)
    tab._run_device_cmd("COM5", "help")

    assert events, "a failed send must reach the Action History bus (was swallowed before)"
    assert events[-1]["status"] == "failed"
    assert events[-1]["port"] == "COM5"


def test_network_device_cmd_publishes_success(qapp, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox

    from src.core.cross_comm import EventBus

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Yes))
    bus = EventBus()
    events: list = []
    bus.subscribe("action.executed", lambda topic, payload: events.append(payload))
    sent: list = []

    tab = _network_tab(lambda p, c: sent.append((p, c)), bus)
    tab._run_device_cmd("COM5", "help")

    assert sent == [("COM5", "help")]
    assert events and events[-1]["status"] == "sent"  # even a successful manual send now reaches history


# ── settings_tab: a tab round-trip must not discard unsaved edits ─────────────────────────────────

def test_settings_showevent_preserves_unsaved_edits(qapp):
    from PyQt5.QtGui import QShowEvent
    from src.ui.qt.settings_tab import SettingsTab

    tab = SettingsTab()
    tab._vault_dir_edit.setText("/my/custom/vault")  # a user edit -> dirty
    assert tab._dirty is True

    tab.showEvent(QShowEvent())  # pre-fix this reloaded from disk and clobbered the edit
    assert tab._vault_dir_edit.text() == "/my/custom/vault"


def test_settings_showevent_reloads_when_clean(qapp, monkeypatch):
    from PyQt5.QtGui import QShowEvent
    from src.ui.qt.settings_tab import SettingsTab

    tab = SettingsTab()
    assert tab._dirty is False
    calls = {"n": 0}
    real = tab._load_into_ui
    monkeypatch.setattr(tab, "_load_into_ui", lambda s: (calls.__setitem__("n", calls["n"] + 1), real(s))[1])

    tab.showEvent(QShowEvent())
    assert calls["n"] == 1  # a clean tab still reloads (no edits to protect)


def test_settings_save_clears_dirty(qapp, monkeypatch):
    import src.ui.qt.settings_tab as st
    from PyQt5.QtWidgets import QMessageBox
    from src.ui.qt.settings_tab import SettingsTab

    monkeypatch.setattr(st, "save_settings", lambda s: None)
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))

    tab = SettingsTab()
    tab._vault_dir_edit.setText("/x")
    assert tab._dirty is True
    tab._on_save()
    assert tab._dirty is False  # persisted -> a reload is safe again
