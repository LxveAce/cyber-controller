"""With Dead Man's Switch enabled, the Qt flash must FAIL SAFE: it must NOT start a plain firmware flash
(which would leave an unprotected board while implying the gate is active). The GUI can't flash the gate
to the device, so it aborts with instructions instead.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core.device_manager import DeviceManager  # noqa: E402
from src.core.flash_engine import FlashEngine  # noqa: E402
from src.core.resources import resource_path  # noqa: E402
from src.ui.qt import flash_tab as FT  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _Combo:
    def findText(self, _t):
        return -1

    def setCurrentIndex(self, _i):
        pass


class _AcceptDlg:
    Accepted = 1

    def __init__(self, *a, **k):
        self.chip = _Combo()
        self.variant = _Combo()

    def exec_(self):
        return 1  # Accepted


def test_qt_dms_enabled_does_not_start_plain_flash(qapp, monkeypatch):
    tab = FT.FlashTab(DeviceManager(), FlashEngine())
    # Select a port + a real profile.
    tab._port_combo.addItem("COM_TEST", "COM_TEST")
    tab._port_combo.setCurrentIndex(tab._port_combo.count() - 1)
    tab._profiles["marauder"] = resource_path("src", "config", "profiles", "marauder.json")
    tab._profile_combo.addItem("marauder")
    tab._profile_combo.setCurrentText("marauder")
    tab._suicide_checkbox.setChecked(True)

    # Auto-accept the DMS setup dialog.
    monkeypatch.setattr("src.ui.qt.suicide_dialog.SuicideSetupDialog", _AcceptDlg, raising=False)

    # Spy: a flash worker must NEVER be constructed on the DMS path.
    started = {"n": 0}
    real_init = FT._FlashWorker.__init__

    def _spy_init(self, *a, **k):
        started["n"] += 1
        real_init(self, *a, **k)

    monkeypatch.setattr(FT._FlashWorker, "__init__", _spy_init)
    monkeypatch.setattr(FT._FlashWorker, "start", lambda self: None)

    tab._on_flash()

    assert started["n"] == 0, "DMS-enabled flash must NOT start a plain firmware flash"
    log = tab._log_output.toPlainText().lower()
    assert "abort" in log and ("unprotected" in log or "deadman-setup" in log)
