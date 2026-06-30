"""Persistent-terminal hardening (bug-hunt #20 mirror dedup, #21 hotplug prune). Offscreen."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import types

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


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


def test_pterm_prunes_dead_connection(qapp, isolated_settings):
    # A hot-unplugged port leaves a dead conn in _pterm_conns; the refresh must prune it so the list
    # doesn't render it as connected and block reconnection.
    win = _make_window()
    try:
        win._pterm_conns["COM_GONE"] = types.SimpleNamespace(is_connected=False)
        win._pterm_port_colors["COM_GONE"] = "#39ff14"
        win._pterm_refresh_ports()
        assert "COM_GONE" not in win._pterm_conns
        assert "COM_GONE" not in win._pterm_port_colors
    finally:
        win.close()


def test_pterm_line_not_mirrored_when_device_tab_owns_same_conn(qapp, isolated_settings):
    # The device tab co-owns the SAME shared connection -> its own on_line already appended this line,
    # so the persistent-terminal mirror must NOT append it again.
    win = _make_window()
    try:
        conn = types.SimpleNamespace(is_connected=True, write=lambda *_a: None)
        win._pterm_conns["COM7"] = conn
        win._device_tab._active_port = "COM7"
        win._device_tab._active_conn = conn
        before = win._device_tab._terminal.toPlainText()
        win._pterm_on_line("COM7", "hello")
        assert win._device_tab._terminal.toPlainText() == before
    finally:
        win.close()


def test_pterm_line_mirrored_when_device_tab_only_selected(qapp, isolated_settings):
    # The device tab has the port SELECTED but doesn't own a connection -> the mirror should append once.
    win = _make_window()
    try:
        conn = types.SimpleNamespace(is_connected=True, write=lambda *_a: None)
        win._pterm_conns["COM7"] = conn
        win._device_tab._active_port = "COM7"
        win._device_tab._active_conn = None
        win._pterm_on_line("COM7", "hello-mirror")
        assert "hello-mirror" in win._device_tab._terminal.toPlainText()
    finally:
        win.close()
