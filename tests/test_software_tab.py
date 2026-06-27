"""Offscreen smoke test for the Qt Software-OS tab. Drive scan is mocked (no hardware/network)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_software_tab_lists_oses_and_drives(qapp, monkeypatch):
    from src.ui.qt import software_tab
    monkeypatch.setattr(software_tab.sd, "detect_sd_cards",
                        lambda *_a, **_k: [{"device": r"\\.\PhysicalDrive9", "name": "USB", "size": 16 << 30}])
    tab = software_tab.SoftwareTab()

    ids = {tab._os_combo.itemData(i) for i in range(tab._os_combo.count())}
    assert {"tails", "kali", "arch"} <= ids
    assert tab._os_desc.text()  # description populated for the initial selection

    assert tab._drive_combo.count() == 1
    assert tab._drive_combo.itemData(0) == r"\\.\PhysicalDrive9"

    # selecting a different OS clears the stale resolved release + refreshes the description
    tab._resolved = object()
    tab._os_combo.setCurrentIndex((tab._os_combo.currentIndex() + 1) % tab._os_combo.count())
    assert tab._resolved is None
