"""The Macro tab must tag a recording with the selected port's firmware (QA-5 #8).

`MacroTab._on_record` used to call `start_recording(port)` with no protocol, so every recorded
macro got `device_protocol=""` ("Protocol: any") no matter what firmware the board ran, and replay
used the generic parser/terminator. This checks the tab looks the firmware up and passes it through.
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


class _Dev:
    def __init__(self, port: str, name: str, fw: str) -> None:
        self.port = port
        self.name = name
        self.firmware = fw


class _FakeDM:
    def __init__(self, rows) -> None:
        self._devs = [_Dev(*r) for r in rows]

    def scan_ports(self):
        return list(self._devs)

    def get_device(self, port: str):
        return next((d for d in self._devs if d.port == port), None)


def test_record_tags_macro_with_device_firmware(qapp, tmp_path):
    from src.core.macro_recorder import MacroRecorder
    from src.ui.qt.macro_tab import MacroTab

    tab = MacroTab(MacroRecorder(macros_dir=tmp_path), _FakeDM([("COM9", "GhostESP", "ghostesp")]))
    tab._port_combo.setCurrentIndex(0)          # the only scanned port
    tab._on_record()

    assert tab._recorder.is_recording
    tab._recorder.record_command("scanap")
    macro = tab._recorder.stop_recording(name="t")
    assert macro.device_protocol == "ghostesp"  # tagged with the port's firmware, not "" / "any"


def test_record_unknown_firmware_falls_back_to_blank(qapp, tmp_path):
    from src.core.macro_recorder import MacroRecorder
    from src.ui.qt.macro_tab import MacroTab

    tab = MacroTab(MacroRecorder(macros_dir=tmp_path), _FakeDM([("COM9", "Unknown board", "")]))
    tab._port_combo.setCurrentIndex(0)
    tab._on_record()

    assert tab._recorder.is_recording
    tab._recorder.record_command("help")
    macro = tab._recorder.stop_recording(name="t")
    assert macro.device_protocol == ""          # no firmware known -> stays generic, no crash
