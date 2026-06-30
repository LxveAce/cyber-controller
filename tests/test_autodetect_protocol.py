"""Auto-detect protocol seeding from board type (bug-hunt #5, partial — safe board-type seed).

A Flipper-board device on 'Auto-detect' must get the Flipper parser (+ CR terminator), not the Marauder
grammar with LF. ESP32 / unknown stays Marauder (the flagship, common case). Offscreen."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _tab_with(board_type):
    from src.core.device_manager import DeviceManager
    from src.core.cross_comm import EventBus, TargetPool
    from src.models.device import Device
    from src.ui.qt.device_tab import DeviceTab
    dm = DeviceManager()
    dm.add_device(Device(port="COM7", name="dev", board_type=board_type))
    tab = DeviceTab(dm, TargetPool(EventBus()), None)
    tab._active_port = "COM7"
    return tab


def test_autodetect_flipper_board_uses_flipper(qapp):
    from src.models.device import BoardType
    tab = _tab_with(BoardType.FLIPPER_ZERO)
    proto = tab._selected_protocol()  # combo defaults to Auto-detect
    assert proto.protocol_name == "flipper"
    assert proto.line_ending == "\r"


def test_autodetect_esp32_board_uses_marauder(qapp):
    from src.models.device import BoardType
    tab = _tab_with(BoardType.ESP32)
    assert tab._selected_protocol().protocol_name == "marauder"


def test_autodetect_no_device_defaults_marauder(qapp):
    from src.core.device_manager import DeviceManager
    from src.core.cross_comm import EventBus, TargetPool
    from src.ui.qt.device_tab import DeviceTab
    tab = DeviceTab(DeviceManager(), TargetPool(EventBus()), None)
    assert tab._selected_protocol().protocol_name == "marauder"
