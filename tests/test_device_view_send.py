"""Device View send-wiring (main_window._device_view_send).

The skin drives the connected device only when its firmware matches, through the safety gate; otherwise it
stays a preview. Offscreen. safety.should_confirm is monkeypatched off so the (non-dangerous) command path
doesn't pop a modal in the test.
"""

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


class _FakeConn:
    def __init__(self):
        self.sent = []

    def write(self, cmd):
        self.sent.append(cmd)


def test_sends_to_matching_active_connection(qapp, isolated_settings, monkeypatch):
    import src.core.safety as safety
    monkeypatch.setattr(safety, "should_confirm", lambda *a, **k: False)
    win = _make_window()
    try:
        conn = _FakeConn()
        win._device_tab._active_conn = conn          # device_tab firmware = Auto-detect -> marauder
        ok = win._device_view_send("marauder", "scanap")
        assert ok is True
        assert conn.sent == ["scanap"]
    finally:
        win.close()


def test_preview_when_no_connection(qapp, isolated_settings):
    win = _make_window()
    try:
        win._device_tab._active_conn = None
        assert win._device_view_send("marauder", "scanap") is False
    finally:
        win.close()


def test_no_cross_firmware_send(qapp, isolated_settings, monkeypatch):
    import src.core.safety as safety
    monkeypatch.setattr(safety, "should_confirm", lambda *a, **k: False)
    win = _make_window()
    try:
        conn = _FakeConn()
        win._device_tab._active_conn = conn          # active firmware resolves to marauder
        # a GhostESP skin command must NOT be sent to a Marauder device
        assert win._device_view_send("ghostesp", "scanap") is False
        assert conn.sent == []
    finally:
        win.close()


@pytest.mark.parametrize("skin_id, proto_cls_path", [
    ("ghostesp", "src.protocols.ghost_esp.GhostESPProtocol"),   # protocol_name "ghost-esp"
    ("esp32div", "src.protocols.esp32_div.Esp32DivProtocol"),   # protocol_name "esp32-div"
])
def test_sends_to_matching_hyphenated_protocol(qapp, isolated_settings, monkeypatch,
                                               skin_id, proto_cls_path):
    """The skin id ('ghostesp'/'esp32div') must map to the device's real protocol_name
    ('ghost-esp'/'esp32-div'), or a correctly-matched device silently never receives the command.

    Regression: _SKIN_PROTOCOL used to map 'ghostesp'->'ghostesp' and 'esp32div'->'esp32_div',
    so the guard comparison never matched and _device_view_send returned False (preview, not sent)."""
    import importlib
    import src.core.safety as safety
    monkeypatch.setattr(safety, "should_confirm", lambda *a, **k: False)

    mod_path, cls_name = proto_cls_path.rsplit(".", 1)
    proto_cls = getattr(importlib.import_module(mod_path), cls_name)
    proto = proto_cls()

    win = _make_window()
    try:
        conn = _FakeConn()
        win._device_tab._active_conn = conn
        # Pin the active device's selected protocol to the real matching firmware protocol.
        monkeypatch.setattr(win._device_tab, "_selected_protocol", lambda: proto)

        ok = win._device_view_send(skin_id, "scanap")
        assert ok is True, "matching device must actually receive the command, not preview"
        assert conn.sent == ["scanap"]
    finally:
        win.close()
