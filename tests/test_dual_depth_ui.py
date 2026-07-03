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


@pytest.fixture(autouse=True)
def _no_blocking_sd_probe(monkeypatch):
    """Building a window constructs SoftwareTab, whose __init__ calls ``sd_backend.detect_sd_cards``
    — on Windows that shells out to ``powershell Get-Disk`` (up to a 15s subprocess timeout) on
    EVERY window built. Across this file's ~9 window constructions the repeated PowerShell spawns
    blow past the harness timeout and look like an indefinite hang (confirmed via faulthandler:
    blocked in subprocess.communicate from SoftwareTab.__init__). These tests never exercise SD
    detection, so stub it to an instant empty result — test isolation only, no feature change."""
    import src.core.backends.sd_backend as sd
    monkeypatch.setattr(sd, "detect_sd_cards", lambda *a, **k: [])


def _make_window():
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.core.cross_comm import EventBus, TargetPool
    from src.ui.qt.main_window import CyberControllerWindow

    bus = EventBus()
    return CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))


def _shown(w) -> bool:
    return not w.isHidden()


def _quiesce(win) -> None:
    """Stop every background activity a freshly-built window starts, so nothing runs during the
    test. A window kicks off a HealthMonitor polling thread and several per-tab QTimers
    (Targets/Broadcast 3s, Cross-Comm 5s, device + sidebar refresh, …). These tests only assert
    static widget state after an explicit ``set_ui_mode`` call — no timer or poll thread is needed
    — and leaving them live lets accumulated timers/threads across the file's windows hang a later
    test's event loop. Quiescing at construction removes the whole class of flakiness."""
    from PyQt5.QtCore import QTimer

    try:
        win._health.stop()
    except Exception:  # noqa: BLE001
        pass
    for timer in win.findChildren(QTimer):
        timer.stop()


@pytest.fixture
def make_window(qapp, isolated_settings):
    """Factory that builds quiesced CyberControllerWindow(s) and GUARANTEES teardown, so no live
    window (its timers / health thread / pop-outs) leaks into the next test."""
    created: list = []

    def _factory():
        win = _make_window()
        created.append(win)
        _quiesce(win)
        return win

    yield _factory

    for win in created:
        try:
            win.close()  # runs the window's own closeEvent cleanup (dm shutdown, re-dock pop-outs)
        except Exception:  # noqa: BLE001 — teardown must never raise
            pass
        win.deleteLater()
    qapp.processEvents()


def test_default_is_pro_full_ui(make_window):
    win = make_window()
    assert win.ui_mode == "pro"
    assert win._act_mode_pro.isChecked() and not win._act_mode_simple.isChecked()
    # advanced groups visible in Pro
    assert _shown(win._flash_tab._vault_card)
    assert _shown(win._flash_tab._suicide_card)
    assert _shown(win._settings_tab._gate_card)
    assert _shown(win._health_tab._disk_gauge)
    assert _shown(win._software_tab._offline_cb)
    assert _shown(win._macro_tab._var_card)
    assert _shown(win._cross_comm_tab._stream_card)
    assert _shown(win._device_tab._cmd_palette)
    assert _shown(win._wardrive_tab._out_card)


def test_simple_streamlines_every_wired_tab(make_window):
    win = make_window()
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
    # Macro
    assert not _shown(win._macro_tab._var_card)
    assert not _shown(win._macro_tab._speed_combo)
    assert not _shown(win._macro_tab._btn_record)
    assert not _shown(win._macro_tab._btn_save)
    assert win._macro_tab._speed_combo.currentText() == "1x"  # locked playback speed
    # Cross-Comm
    assert not _shown(win._cross_comm_tab._stream_card)
    assert not _shown(win._cross_comm_tab._rules_card)
    assert not _shown(win._cross_comm_tab._action_card)
    # Device
    assert not _shown(win._device_tab._firmware_combo)
    assert not _shown(win._device_tab._cmd_palette)
    # Wardrive
    assert not _shown(win._wardrive_tab._dev_baud)
    assert not _shown(win._wardrive_tab._out_card)


def test_badge_and_ctrl_m_toggle(make_window):
    win = make_window()
    win.set_ui_mode("simple")
    assert "Simple" in win._mode_badge.text()
    win._toggle_ui_mode()  # the Ctrl+M handler
    assert win.ui_mode == "pro"
    assert "Pro" in win._mode_badge.text()
    assert _shown(win._flash_tab._vault_card)


def test_mode_persists_and_loads_on_startup(make_window, isolated_settings):
    win = make_window()
    win.set_ui_mode("simple")
    assert isolated_settings.load_settings()["interface"]["mode"] == "simple"
    win2 = make_window()  # a fresh window must pick up the saved mode
    assert win2.ui_mode == "simple"
    assert not _shown(win2._flash_tab._vault_card)


# ── Track B UX #5: Simple/Pro on Targets / Broadcast / Network ───────────────

def test_pro_shows_targets_broadcast_network_full(make_window):
    win = make_window()  # default Pro
    assert win.ui_mode == "pro"
    # Targets — advanced columns present, Clear All button shown.
    tt = win._targets_tab
    assert not any(tt._table.isColumnHidden(c) for c in tt._ADVANCED_COLUMNS)
    assert _shown(tt._clear_btn)
    # Broadcast — offensive attack verbs shown.
    bb = win._broadcast_bar
    assert bb._advanced_buttons  # there are attack verbs (Deauth / Beacon Spam / BLE Spam)
    assert all(_shown(b) for b in bb._advanced_buttons)
    # Network — the experimental graph + its controls are shown, the Simple notice hidden.
    nt = win._network_tab
    assert _shown(nt._view) and _shown(nt._controls)
    assert not _shown(nt._simple_notice)


def test_simple_streamlines_targets_broadcast_network(make_window):
    win = make_window()
    win.set_ui_mode("simple")
    assert win.ui_mode == "simple"
    # Targets — abbreviated technical columns + bulk-destructive Clear All are hidden.
    tt = win._targets_tab
    assert all(tt._table.isColumnHidden(c) for c in tt._ADVANCED_COLUMNS)
    assert not _shown(tt._clear_btn)
    # Broadcast — offensive attack verbs hidden (STOP ALL + scan verbs stay).
    bb = win._broadcast_bar
    assert all(not _shown(b) for b in bb._advanced_buttons)
    assert _shown(bb._stop_btn)  # STOP ALL is never hidden
    # Network — the experimental send-capable graph is hidden behind a notice.
    nt = win._network_tab
    assert not _shown(nt._view) and not _shown(nt._controls)
    assert _shown(nt._simple_notice)


def test_simple_pro_roundtrip_restores_the_three_tabs(make_window):
    win = make_window()
    win.set_ui_mode("simple")
    win.set_ui_mode("pro")  # Pro must restore everything Simple hid
    tt = win._targets_tab
    assert not any(tt._table.isColumnHidden(c) for c in tt._ADVANCED_COLUMNS)
    assert _shown(tt._clear_btn)
    assert all(_shown(b) for b in win._broadcast_bar._advanced_buttons)
    assert _shown(win._network_tab._view)
    assert not _shown(win._network_tab._simple_notice)


# ── Track B UX #4: discoverability affordance (command palette in a menu) ─────

def _find_action(win, needle: str):
    """Any menu action whose (accelerator-stripped) text contains `needle`."""
    for top in win.menuBar().actions():
        menu = top.menu()
        if menu is None:
            continue
        for act in menu.actions():
            if needle.lower() in act.text().replace("&", "").lower():
                return act
    return None


def test_command_palette_has_menu_affordance(make_window):
    from PyQt5.QtGui import QKeySequence

    win = make_window()
    act = _find_action(win, "Command Palette")
    assert act is not None, "expected a Help/View menu entry for the Command Palette"
    # The shortcut is surfaced on the entry so users learn Ctrl+Shift+P without guessing.
    assert act.shortcut() == QKeySequence("Ctrl+Shift+P")
    assert act.statusTip(), "the palette entry should carry a status-bar hint"
