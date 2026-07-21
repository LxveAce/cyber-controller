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
    monkeypatch.setattr(win._dm, "open_connection", lambda port, baud=115200, owner=None: conn)
    monkeypatch.setattr(win._dm, "get_device", lambda port: dev)
    monkeypatch.setattr(win, "_pterm_checked_ports", lambda: ["COM7"])
    monkeypatch.setattr(win, "_pterm_refresh_ports", lambda: None)
    monkeypatch.setattr(win, "_refresh_sidebar_devices", lambda: None)


def test_pterm_connect_stamps_default_firmware(qapp, isolated_settings, monkeypatch):
    # Connecting a board in the terminal must stamp a firmware via the CENTRAL setter (set_firmware),
    # not a direct dev.firmware write — that's what fires on_device_changed so the Broadcast panel
    # repopulates reactively instead of waiting on its safety-net timer.
    win = _make_window()
    try:
        dev = types.SimpleNamespace(firmware="", board_type=None)
        _stub_connect(win, monkeypatch, dev)
        calls = []
        monkeypatch.setattr(win._dm, "set_firmware", lambda port, fw, **kw: calls.append((port, fw)))
        win._pterm_on_connect()
        assert calls == [("COM7", "marauder")]
    finally:
        win.close()


def test_pterm_connect_does_not_clobber_explicit_firmware(qapp, isolated_settings, monkeypatch):
    # An explicit firmware (e.g. chosen in the Devices tab) must survive a terminal connect — the
    # blank-only guard means set_firmware is never invoked when a firmware is already set.
    win = _make_window()
    try:
        dev = types.SimpleNamespace(firmware="ghost_esp", board_type=None)
        _stub_connect(win, monkeypatch, dev)
        calls = []
        monkeypatch.setattr(win._dm, "set_firmware", lambda port, fw, **kw: calls.append((port, fw)))
        win._pterm_on_connect()
        assert calls == []
        assert dev.firmware == "ghost_esp"
    finally:
        win.close()


def test_pterm_send_stamps_flipper_cr_terminator(qapp, isolated_settings):
    # A Flipper connected in the persistent terminal must receive a CR-terminated command. The connection
    # is built with LF (open_connection seeds the terminator from Device.firmware, still blank when the
    # terminal opens the port), and the Flipper CLI silently ignores LF — so _pterm_on_send must re-stamp
    # the terminator from the device's persisted firmware right before writing (mirrors device_tab._on_send).
    from src.models.device import Device

    win = _make_window()
    try:
        win._dm.add_device(Device(port="COM7", name="Flipper", firmware="flipper", connected=True))

        class _Conn:
            def __init__(self):
                self.line_ending = "\n"  # what open_connection seeded before firmware was known
                self.writes = []

            def write(self, s):
                self.writes.append((s, self.line_ending))

        conn = _Conn()
        win._pterm_conns["COM7"] = conn
        win._pterm_port_colors["COM7"] = "#39ff14"
        win._pterm_checked_ports = lambda: ["COM7"]  # both checked and connected
        win._pterm_input.setText("help")
        win._pterm_on_send()
        assert conn.line_ending == "\r", "Flipper CLI needs CR; an LF-terminated command is silently ignored"
        assert conn.writes and conn.writes[-1] == ("help", "\r")
    finally:
        win.close()


def test_pterm_send_keeps_lf_for_marauder(qapp, isolated_settings):
    # The re-stamp must be per-firmware, not a blanket CR: a Marauder must still get LF.
    from src.models.device import Device

    win = _make_window()
    try:
        win._dm.add_device(Device(port="COM8", name="Marauder", firmware="marauder", connected=True))

        class _Conn:
            def __init__(self):
                self.line_ending = "\n"
                self.writes = []

            def write(self, s):
                self.writes.append((s, self.line_ending))

        conn = _Conn()
        win._pterm_conns["COM8"] = conn
        win._pterm_port_colors["COM8"] = "#39ff14"
        win._pterm_checked_ports = lambda: ["COM8"]
        win._pterm_input.setText("scanap")
        win._pterm_on_send()
        assert conn.line_ending == "\n"
        assert conn.writes and conn.writes[-1] == ("scanap", "\n")
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


def test_pterm_reflects_activity_bus(qapp, isolated_settings):
    # A line on the activity bus (flash/crack/broadcast/cmd/macro) surfaces in the persistent
    # terminal with a [source] tag — reflecting non-serial activity, not just serial RX.
    from src.core.activity_log import activity_log
    win = _make_window()
    try:
        activity_log().emit_line("flash", "Writing at 0x10000...", "info")
        text = win._pterm_output.toPlainText()
        assert "[flash]" in text
        assert "Writing at 0x10000..." in text
    finally:
        win.close()


def test_pterm_activity_line_html_escaped(qapp, isolated_settings):
    # Tool/device output on the bus is untrusted; the activity slot must html.escape it so a
    # crafted line can't forge terminal markup (a fake green KEY FOUND banner on a security tool).
    from src.core.activity_log import activity_log
    win = _make_window()
    try:
        activity_log().emit_line("crack", "<span style='color:#3fb950'>KEY FOUND</span>", "info")
        text = win._pterm_output.toPlainText()
        assert "<span" in text, "untrusted markup must be escaped to literal text, not rendered"
    finally:
        win.close()


def test_pterm_target_serial_forces_device_send(qapp, isolated_settings):
    # With the send-target set to Device(s), a line whose first word is a known tool name (aircrack-ng)
    # must still be WRITTEN to the connected board, not launched as a local tool.
    from src.models.device import Device

    win = _make_window()
    try:
        win._dm.add_device(Device(port="COM7", name="Marauder", firmware="marauder", connected=True))

        class _Conn:
            def __init__(self):
                self.line_ending = "\n"
                self.writes = []

            def write(self, s):
                self.writes.append(s)

        conn = _Conn()
        win._pterm_conns["COM7"] = conn
        win._pterm_port_colors["COM7"] = "#39ff14"
        win._pterm_checked_ports = lambda: ["COM7"]
        win._pterm_target.setCurrentIndex(win._pterm_target.findData("serial"))
        win._pterm_input.setText("aircrack-ng")  # a known tool name, but the target is a device
        win._pterm_on_send()
        assert conn.writes == ["aircrack-ng"], "Device(s) target must write to the board, not run the tool"
    finally:
        win.close()


def test_pterm_target_computer_refuses_non_tool(qapp, isolated_settings):
    # With the send-target set to Computer, a first word that isn't a bundled tool is refused and never
    # leaked to a connected device (the tool shell is scoped, not a general OS shell).
    from src.models.device import Device

    win = _make_window()
    try:
        win._dm.add_device(Device(port="COM7", name="Marauder", firmware="marauder", connected=True))

        class _Conn:
            def __init__(self):
                self.line_ending = "\n"
                self.writes = []

            def write(self, s):
                self.writes.append(s)

        conn = _Conn()
        win._pterm_conns["COM7"] = conn
        win._pterm_checked_ports = lambda: ["COM7"]
        win._pterm_target.setCurrentIndex(win._pterm_target.findData("computer"))
        win._pterm_input.setText("reboot")  # not a bundled tool
        win._pterm_on_send()
        assert conn.writes == [], "Computer target must not leak a non-tool word to a device"
        assert "not a bundled tool" in win._pterm_output.toPlainText()
    finally:
        win.close()


def test_device_tab_rx_echoes_to_bottom_terminal(qapp, isolated_settings):
    # A device connected only on the Devices tab must still surface its RETURNS in the always-visible
    # bottom terminal (via the activity bus), so the bottom terminal reflects "every return". The bottom
    # terminal does NOT own COM9 here.
    win = _make_window()
    try:
        win._device_tab._on_line_received("COM9", "AP: HomeNet ch 6")
        text = win._pterm_output.toPlainText()
        assert "AP: HomeNet ch 6" in text
        assert "[COM9]" in text
    finally:
        win.close()


def test_device_tab_rx_not_double_echoed_when_bottom_owns_port(qapp, isolated_settings):
    # When the bottom terminal already owns the port it renders that RX itself (via _pterm_on_line), so
    # the Devices-tab echo must be suppressed — otherwise the same line shows twice at the bottom.
    import types

    win = _make_window()
    try:
        win._pterm_conns["COM9"] = types.SimpleNamespace(is_connected=True, write=lambda *_a: None)
        win._device_tab._on_line_received("COM9", "AP: OwnedNet")
        # The device-tab echo path is guarded off for co-owned ports, and _pterm_on_line wasn't called
        # in this direct-call test, so the line must NOT appear via the bus here.
        assert "AP: OwnedNet" not in win._pterm_output.toPlainText()
    finally:
        win.close()


def test_activity_log_emit_shape_and_blank_drop(qapp):
    # emit_line drops a blank line (callers can emit unconditionally), defaults/normalizes the
    # level, and delivers (source, level, text). Uses a fresh ActivityLog (not the singleton) so
    # the test's connection doesn't leak into other tests.
    from src.core.activity_log import ActivityLog
    bus = ActivityLog()
    seen = []
    bus.line.connect(lambda s, lvl, t: seen.append((s, lvl, t)))
    bus.emit_line("flash", "")                    # blank -> dropped
    bus.emit_line("flash", "hello")               # default level -> info
    bus.emit_line("crack", "bad", "nonsense")     # unknown level -> normalized to info
    bus.emit_line("crack", "err", "error")        # valid level preserved
    assert seen == [("flash", "info", "hello"), ("crack", "info", "bad"), ("crack", "error", "err")]
