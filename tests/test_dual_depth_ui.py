"""Dual-depth GUI (Simple / Pro progressive disclosure).

Verifies the core toggle (menu radio + status badge + Ctrl+M + persistence + startup-load) and the
per-tab streamlining (Flash / Settings / Health / Software). Pro == today's full UI (default, zero
regression); Simple hides advanced widget groups via each tab's set_ui_mode().

Uses ``isHidden()`` (the widget's own visibility request) rather than ``isVisible()`` because the
latter is False for widgets living on a non-current QTabWidget page.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    import src.config.settings as S
    monkeypatch.setattr(S, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(S, "SETTINGS_PATH", tmp_path / "settings.json")
    return S


def _make_window():
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.core.cross_comm import EventBus, TargetPool
    from src.ui.qt.main_window import CyberControllerWindow

    bus = EventBus()
    return CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))


def _shown(w) -> bool:
    return not w.isHidden()


def test_default_is_pro_full_ui(qapp, isolated_settings):
    win = _make_window()
    assert win.ui_mode == "pro"
    assert win._act_mode_pro.isChecked() and not win._act_mode_simple.isChecked()
    # advanced groups visible in Pro
    assert _shown(win._flash_tab._vault_card)
    assert _shown(win._flash_tab._suicide_card)
    assert _shown(win._settings_tab._gate_card)
    assert _shown(win._health_tab._disk_gauge)
    assert _shown(win._software_tab._offline_cb)


def test_simple_streamlines_every_wired_tab(qapp, isolated_settings):
    win = _make_window()
    win.set_ui_mode("simple")
    assert win.ui_mode == "simple"
    assert win._act_mode_simple.isChecked() and not win._act_mode_pro.isChecked()
    # Flash
    assert not _shown(win._flash_tab._vault_card)
    assert not _shown(win._flash_tab._queue_card)
    assert not _shown(win._flash_tab._suicide_card)
    assert not _shown(win._flash_tab._btn_browse)
    assert not win._flash_tab._suicide_checkbox.isChecked()  # hidden DMS forced off
    # Settings
    assert not _shown(win._settings_tab._comm_card)
    assert not _shown(win._settings_tab._gate_card)
    assert not _shown(win._settings_tab._secure_card)
    # Health
    assert not _shown(win._health_tab._disk_gauge)
    assert not _shown(win._health_tab._batt_gauge)
    assert not _shown(win._health_tab._dev_card)
    # Software
    assert not _shown(win._software_tab._offline_cb)
    assert not _shown(win._software_tab._btn_local)
    assert win._software_tab._offline_cb.isChecked() is False  # always online in Simple


def test_badge_and_ctrl_m_toggle(qapp, isolated_settings):
    win = _make_window()
    win.set_ui_mode("simple")
    assert "Simple" in win._mode_badge.text()
    win._toggle_ui_mode()  # the Ctrl+M handler
    assert win.ui_mode == "pro"
    assert "Pro" in win._mode_badge.text()
    assert _shown(win._flash_tab._vault_card)


def test_mode_persists_and_loads_on_startup(qapp, isolated_settings):
    win = _make_window()
    win.set_ui_mode("simple")
    assert isolated_settings.load_settings()["interface"]["mode"] == "simple"
    win2 = _make_window()  # a fresh window must pick up the saved mode
    assert win2.ui_mode == "simple"
    assert not _shown(win2._flash_tab._vault_card)
