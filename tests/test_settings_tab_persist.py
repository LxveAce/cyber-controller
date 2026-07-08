"""Saving the Settings tab must NOT wipe the interface mode + loadout or re-arm the first-run
choosers, and must NOT revert a setting another flow persisted after the tab was shown. Regression:
_gather rebuilt the whole settings dict from the long-lived in-memory snapshot, so a Save reset the
non-widget sections (interface mode + loadout, the acks, the update-suppression bookkeeping) to that
stale snapshot — undoing a Simple choice, a loadout, or a just-dismissed update prompt. _gather now
re-reads disk and overlays only widget-backed keys. Offscreen Qt."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Point settings persistence at a temp file so the tab reads/writes a hermetic on-disk state."""
    from src.config import settings as S
    path = tmp_path / "settings.json"
    monkeypatch.setattr(S, "SETTINGS_PATH", path)
    monkeypatch.setattr(S, "SETTINGS_DIR", tmp_path)
    return path


def test_settings_save_preserves_interface_and_acks(qapp, isolated_settings):
    from src.config import settings as S
    from src.config.settings import DEFAULTS, _deep_merge
    from src.ui.qt.settings_tab import SettingsTab

    # A Simple-mode + de-bloat loadout choice already persisted to disk (as View ▸ Loadout / Ctrl+M do).
    S.save_settings({
        "interface": {"mode": "simple", "loadout": {
            "full_stack": False, "configured": True,
            "firmwares": ["marauder"], "hardware": ["esp32"]}},
        "_interface_mode_ack": True,
        "_disclaimer_ack": True,
    })

    tab = SettingsTab()
    gathered = tab._gather()
    assert gathered["interface"]["mode"] == "simple"
    assert gathered["interface"]["loadout"]["configured"] is True
    assert gathered["_interface_mode_ack"] is True
    assert gathered["_disclaimer_ack"] is True

    merged = _deep_merge(DEFAULTS, gathered)  # what save_settings actually persists
    assert merged["interface"]["mode"] == "simple"
    assert "loadout" in merged["interface"]
    assert merged["_interface_mode_ack"] is True


def test_save_does_not_revert_a_concurrent_disk_write(qapp, isolated_settings):
    """A Save AFTER another in-process flow wrote settings.json (e.g. a 'Check now' that recorded
    'Don't show again' via a modal that fired no showEvent on the tab) must NOT revert it. _gather
    re-reads disk, so the concurrent suppression survives; only the widget-backed key is overlaid."""
    from src.config import settings as S
    from src.ui.qt.settings_tab import SettingsTab

    S.save_settings({"updates": {"enabled": True, "suppressed": False, "dismissed_version": ""}})

    tab = SettingsTab()   # snapshots the pre-suppression state on construct (no showEvent in tests)

    # concurrent flow: user ticks "Don't show again" on an update prompt -> written straight to disk
    cur = S.load_settings()
    cur["updates"]["suppressed"] = True
    cur["updates"]["dismissed_version"] = "v9.9.9"
    S.save_settings(cur)

    gathered = tab._gather()   # a plain Save on the still-visible tab
    assert gathered["updates"]["suppressed"] is True          # concurrent write NOT reverted
    assert gathered["updates"]["dismissed_version"] == "v9.9.9"
    assert gathered["updates"]["enabled"] is True             # widget-backed key still applied
