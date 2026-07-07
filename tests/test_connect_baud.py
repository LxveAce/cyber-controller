"""The user-configured Default Baud Rate (Settings ▸ Serial ▸ serial.default_baud) must flow through the
Qt connect paths — both the Devices tab and the persistent terminal. Regression: both omitted the baud
argument, so open_connection fell back to its hardcoded 115200 and a device that talks at a non-default
baud (e.g. 9600 or 230400) connected at the wrong speed → garbled TX/RX. Offscreen Qt; settings live in a
temp file and open_connection is stubbed so no real port is opened."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings_baud(tmp_path, monkeypatch):
    """Point settings at a temp file carrying a non-default baud; return the configured value."""
    import src.config.settings as S

    baud = 9600
    monkeypatch.setattr(S, "SETTINGS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(S, "SETTINGS_PATH", tmp_path / "settings.json", raising=False)
    (tmp_path / "settings.json").write_text(
        json.dumps({"serial": {"default_baud": baud}}), encoding="utf-8"
    )
    return baud


class _Conn:
    is_connected = True

    def on_line(self, *_a, **_k):
        pass

    def write(self, *_a, **_k):
        pass


def test_device_tab_connect_uses_configured_baud(qapp, settings_baud, monkeypatch):
    from src.core.device_manager import DeviceManager
    from src.models.device import Device
    from src.ui.qt.device_tab import DeviceTab

    captured: dict[str, int] = {}
    dm = DeviceManager()
    dm.add_device(Device(port="COM9"))
    monkeypatch.setattr(
        dm,
        "open_connection",
        lambda port, baud=115200, owner=None: captured.update(baud=baud) or _Conn(),
    )

    tab = DeviceTab(dm)
    tab._active_port = "COM9"
    tab._on_connect()

    assert captured.get("baud") == settings_baud, (
        "Devices-tab connect must pass the configured serial.default_baud, not the hardcoded 115200"
    )


def test_pterm_connect_uses_configured_baud(qapp, settings_baud, monkeypatch):
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.models.device import Device
    from src.ui.qt.main_window import CyberControllerWindow

    captured: dict[str, int] = {}
    bus = EventBus()
    dm = DeviceManager()
    dm.add_device(Device(port="COM7"))
    win = CyberControllerWindow(dm, FlashEngine(), bus, TargetPool(bus))
    try:
        monkeypatch.setattr(
            win._dm,
            "open_connection",
            lambda port, baud=115200, owner=None: captured.update(baud=baud) or _Conn(),
        )
        monkeypatch.setattr(win, "_pterm_checked_ports", lambda: ["COM7"])
        monkeypatch.setattr(win, "_pterm_refresh_ports", lambda: None)
        monkeypatch.setattr(win, "_refresh_sidebar_devices", lambda: None)
        win._pterm_on_connect()
        assert captured.get("baud") == settings_baud, (
            "Persistent-terminal connect must pass the configured serial.default_baud, not 115200"
        )
    finally:
        win.close()
