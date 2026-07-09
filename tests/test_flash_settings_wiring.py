"""The user-configured Flash Baud Rate (Settings ▸ Flash ▸ flash.flash_baud) must reach the flash path.

Regression: SettingsTab persisted a whole 'Flash Defaults' card (Flash Baud / Flash Mode / Verify /
Auto-backup) into settings.json, but NOTHING consumed settings['flash'] — flash_tab drove flash_engine
off the per-board PROFILE baud, so lowering the Flash Baud to make a marginal CH340K / long-cable ESP32
flash reliably had zero effect. The Flash Mode / Verify / Auto-backup controls had no reachable consumer
at all, so they were removed (false affordances) rather than left implying an effect they never had.

Offscreen Qt; settings live in a temp file; the flash worker is stubbed so no real port/network is used.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core.device_manager import DeviceManager  # noqa: E402
from src.core.flash_engine import FlashEngine  # noqa: E402
from src.core.resources import resource_path  # noqa: E402
from src.ui.qt import flash_tab as FT  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def flash_settings(tmp_path, monkeypatch):
    """Point settings at a temp file carrying a Flash Baud distinct from the profile default (921600)."""
    import src.config.settings as S

    baud = 115200
    monkeypatch.setattr(S, "SETTINGS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(S, "SETTINGS_PATH", tmp_path / "settings.json", raising=False)
    (tmp_path / "settings.json").write_text(
        json.dumps({"flash": {"flash_baud": baud}}), encoding="utf-8"
    )
    return baud


def test_flash_uses_configured_flash_baud(qapp, flash_settings, monkeypatch):
    tab = FT.FlashTab(DeviceManager(), FlashEngine())
    # Select a port + a real profile (marauder pins no baud, so it loads at the 921600 default).
    tab._port_combo.addItem("COM_TEST", "COM_TEST")
    tab._port_combo.setCurrentIndex(tab._port_combo.count() - 1)
    tab._profiles["marauder"] = resource_path("src", "config", "profiles", "marauder.json")
    tab._profile_combo.addItem("marauder")
    tab._profile_combo.setCurrentText("marauder")
    # Dead Man's Switch OFF (default) so the plain flash path runs.

    # Capture the profile handed to the flash worker; never actually start it.
    captured: dict = {}
    real_init = FT._FlashWorker.__init__

    def _spy_init(self, engine, port, profile, *a, **k):
        captured["profile"] = profile
        real_init(self, engine, port, profile, *a, **k)

    monkeypatch.setattr(FT._FlashWorker, "__init__", _spy_init)
    monkeypatch.setattr(FT._FlashWorker, "start", lambda self: None)

    # A blind Marauder+Auto flash now confirms first (B2 flash-default honesty gate) — auto-accept it so
    # this test exercises the baud path, not the dialog. See test_flash_default_variant_gate.py.
    from PyQt5.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Yes))

    tab._on_flash()

    assert captured.get("profile") is not None, "the plain flash path must construct a flash worker"
    assert captured["profile"].baud == flash_settings, (
        "the flash must run at the user-configured flash.flash_baud (115200), not the profile default 921600"
    )


def test_settings_tab_has_no_inert_flash_controls(qapp):
    """The Flash Mode / Verify / Auto-backup toggles and the Connection Timeout spin had no consumer in the
    Qt app; they were removed so the UI can't imply an effect it doesn't have. Only the wired baud controls
    remain, and _gather emits only those (deep-merge restores the vestigial keys from DEFAULTS on save)."""
    from src.ui.qt.settings_tab import SettingsTab

    tab = SettingsTab()
    for attr in ("_timeout_spin", "_flash_mode_combo", "_verify_check", "_backup_check"):
        assert not hasattr(tab, attr), f"{attr} is an inert control and must not be present in Settings"

    gathered = tab._gather()
    assert "default_baud" in gathered["serial"]
    assert "flash_baud" in gathered["flash"]
    # The removed, unconsumed keys are no longer emitted by the UI.
    assert "timeout" not in gathered["serial"]
    assert "verify" not in gathered["flash"]
    assert "auto_backup" not in gathered["flash"]
    assert "mode" not in gathered["flash"]
