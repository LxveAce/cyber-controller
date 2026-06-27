"""Offscreen smoke test for the Wardrive Qt tab. Serial-port enumeration is mocked."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_wardrive_tab(qapp, monkeypatch):
    from src.ui.qt import wardrive_tab
    monkeypatch.setattr(wardrive_tab, "_list_serial_ports",
                        lambda: [("COM5", "USB Serial"), ("COM6", "GPS")])
    tab = wardrive_tab.WardriveTab()
    assert tab._dev_combo.count() == 2
    assert tab._gps_combo.count() == 3  # "(none)" + 2 ports
    assert tab._gps_combo.itemData(0) is None
    assert tab._out_edit.text().endswith(".csv")
