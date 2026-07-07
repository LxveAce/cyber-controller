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


def _stub_connect(win, monkeypatch, dev):
    """Wire the window so _pterm_on_connect connects COM7 to a stub conn + the given device."""
    conn = types.SimpleNamespace(is_connected=True, on_line=lambda *_a: None, write=lambda *_a: None)
    monkeypatch.setattr(win._dm, "open_connection", lambda port, owner=None: conn)
    monkeypatch.setattr(win._dm, "get_device", lambda port: dev)
    monkeypatch.setattr(win, "_pterm_checked_ports", lambda: ["COM7"])
    monkeypatch.setattr(win, "_pterm_refresh_ports", lambda: None)
    monkeypatch.setattr(win, "_refresh_sidebar_devices", lambda: None)


def test_pterm_connect_stamps_default_firmware(qapp, isolated_settings, monkeypatch):
    # Connecting a board in the terminal must stamp a firmware so the Operate surface (Broadcast/Targets/
    # STOP-ALL, which all route by Device.firmware) can send — a blank routes to zero actions and no-ops.
    win = _make_window()
    try:
        dev = types.SimpleNamespace(firmware="", board_type=None)
        _stub_connect(win, monkeypatch, dev)
        win._pterm_on_connect()
        assert dev.firmware == "marauder"
    finally:
        win.close()


def test_pterm_connect_does_not_clobber_explicit_firmware(qapp, isolated_settings, monkeypatch):
    # An explicit firmware (e.g. chosen in the Devices tab) must survive a terminal connect.
    win = _make_window()
    try:
        dev = types.SimpleNamespace(firmware="ghost_esp", board_type=None)
        _stub_connect(win, monkeypatch, dev)
        win._pterm_on_connect()
        assert dev.firmware == "ghost_esp"
    finally:
        win.close()


def test_pterm_line_html_escaped_no_spoof(qapp, isolated_settings):
    # A rogue/impersonating board must not be able to inject HTML into the persistent terminal
    # (QTextEdit.append renders rich text). The device markup must survive as LITERAL text, not
    # be parsed away — otherwise it could forge e.g. a green [DMS] Authenticated banner.
    win = _make_window()
    try:
        conn = types.SimpleNamespace(is_connected=True, write=lambda *_a: None)
        win._pterm_conns["COM7"] = conn
        payload = '</span><b>PWNED</b><span style="color:#3fb950;">[DMS] Authenticated: evil</span>'
        win._pterm_on_line("COM7", payload)
        text = win._pterm_output.toPlainText()
        # If the line were parsed as HTML, the angle-bracket markup would be stripped from plain text.
        assert payload in text, "device serial line must be shown verbatim, not rendered as HTML"
        assert "<b>PWNED</b>" in text
    finally:
        win.close()


def test_dms_auth_result_message_html_escaped(qapp, isolated_settings):
    # The DMS auth-status banner interpolates the raw device line (message); it must be escaped so a
    # rogue board can't inject markup into the operator's authentication banner.
    win = _make_window()
    try:
        win._dms_auth_result(True, '<b>trusted</b><img src=x>')
        text = win._pterm_output.toPlainText()
        assert '<b>trusted</b><img src=x>' in text, "DMS message must be shown verbatim, not as HTML"
    finally:
        win.close()
