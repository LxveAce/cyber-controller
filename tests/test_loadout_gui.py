"""Loadout GUI wiring — apply_loadout hides/shows the right tabs on the real window, and the picker
dialog returns the right loadout. Offscreen.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.config import loadout as L  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    import src.config.settings as S
    monkeypatch.setattr(S, "SETTINGS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(S, "SETTINGS_PATH", tmp_path / "settings.json", raising=False)
    return S


def _make_window():
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.core.cross_comm import EventBus, TargetPool
    from src.ui.qt.main_window import CyberControllerWindow
    bus = EventBus()
    return CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))


def _labels(win):
    return [win._tabs.tabText(i) for i in range(win._tabs.count())]


def test_default_window_shows_all_tabs(qapp, isolated_settings):
    win = _make_window()
    try:
        assert len(_labels(win)) == len(L.TAB_ORDER)  # unconfigured -> fail-open -> everything
    finally:
        win.close()


def test_apply_loadout_hides_unused_tabs(qapp, isolated_settings):
    win = _make_window()
    try:
        lo = {"full_stack": False, "configured": True, "firmwares": ["meshtastic"], "hardware": []}
        win.apply_loadout(lo, persist=False)
        labels = _labels(win)
        # P2.5 verb IA: every top-level surface is a job-verb, and all verbs are ALWAYS (fail-open), so a
        # mesh-only loadout hides no top-level SURFACE — the loadout only ever grouped the sub-views. This
        # locks the grouping invariant: sub-views are never top-level, and all 5 verbs + Settings stay
        # reachable. ("Analyze"/"Network" are gone — dissolved into HUNT/CRACK/RIG.)
        assert "Network" not in labels and "Analyze" not in labels
        for subview in ("Devices", "Health", "Software OS", "Targets", "Control", "Macros",
                        "Wardrive", "Cross-Comm", "Crack Lab", "Flock Map", "Graph"):
            assert subview not in labels  # grouped into a verb surface, never a top-level label
        for core in ("DEVICE", "HUNT", "OPERATE", "CRACK", "MAP", "Settings"):
            assert core in labels
        # Full Stack restores everything
        win.apply_loadout(L.full_stack_loadout(), persist=False)
        assert len(_labels(win)) == len(L.TAB_ORDER)
    finally:
        win.close()


def test_loadout_gps_and_usb_os_gates(qapp, isolated_settings):
    win = _make_window()
    try:
        lo = {"full_stack": False, "configured": True,
              "firmwares": ["marauder"], "hardware": ["esp32", "gps", "usb_os"]}
        win.apply_loadout(lo, persist=False)
        labels = _labels(win)
        # P2.5: Wardrive is a MAP sub-view, Targets a HUNT sub-view, Software OS a RIG sub-view. At top level
        # we assert the always-shown verbs that hold them (MAP / HUNT / RIG) are present.
        assert "MAP" in labels and "HUNT" in labels and "DEVICE" in labels
        for sub in ("Wardrive", "Targets", "Software OS"):
            assert sub not in labels  # sub-views, not top-level
    finally:
        win.close()


def test_apply_loadout_persists(qapp, isolated_settings):
    win = _make_window()
    try:
        lo = {"full_stack": False, "configured": True, "firmwares": ["marauder"], "hardware": ["esp32"]}
        win.apply_loadout(lo, persist=True)
        saved = isolated_settings.load_settings().get("interface", {}).get("loadout")
        assert saved and saved["configured"] and saved["firmwares"] == ["marauder"]
    finally:
        win.close()


def test_dialog_build_result(qapp):
    from src.ui.qt.loadout_dialog import LoadoutDialog
    dlg = LoadoutDialog(current={"firmwares": ["marauder"], "hardware": ["esp32"], "configured": True})
    fs = dlg.build_result(full_stack=True)
    assert fs["full_stack"] and fs["configured"]
    sel = dlg.build_result(full_stack=False)
    assert "marauder" in sel["firmwares"] and sel["configured"] and not sel["full_stack"]
