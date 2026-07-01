"""Saving the Settings tab must NOT wipe the interface mode + loadout or re-arm the first-run
choosers. Regression: _gather rebuilt the whole settings dict without the interface section, so
save_settings' deep-merge reset mode to 'pro', dropped the loadout, and reset _interface_mode_ack —
undoing the user's Simple choice + de-bloat loadout on the most common action (Save). Offscreen Qt."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_settings_save_preserves_interface_and_acks(qapp):
    from src.ui.qt.settings_tab import SettingsTab
    from src.config.settings import DEFAULTS, _deep_merge

    tab = SettingsTab()
    tab._settings = {
        "interface": {"mode": "simple", "loadout": {
            "full_stack": False, "configured": True,
            "firmwares": ["marauder"], "hardware": ["esp32"]}},
        "_interface_mode_ack": True,
        "_disclaimer_ack": True,
    }
    gathered = tab._gather()
    assert gathered["interface"]["mode"] == "simple"
    assert gathered["interface"]["loadout"]["configured"] is True
    assert gathered["_interface_mode_ack"] is True

    merged = _deep_merge(DEFAULTS, gathered)  # what save_settings actually persists
    assert merged["interface"]["mode"] == "simple"
    assert "loadout" in merged["interface"]
    assert merged["_interface_mode_ack"] is True
