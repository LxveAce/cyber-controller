"""'Clear Terminal' must clear the terminal the user is actually looking at.

Regression: the command-palette "Clear Terminal" cleared ONLY the Devices sub-tab's terminal, never the
always-visible bottom persistent panel (``_pterm_output``) — so it appeared to do nothing whenever that
panel was on screen (most of the time). Offscreen Qt, mirrors the fixture pattern in
test_sidebar_device_sync.py.
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
    monkeypatch.setattr(S, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(S, "SETTINGS_PATH", tmp_path / "settings.json")
    return S


@pytest.fixture(autouse=True)
def _no_blocking_sd_probe(monkeypatch):
    """SoftwareTab.__init__ shells out for SD detection; stub it so building a window is instant."""
    import src.core.backends.sd_backend as sd
    monkeypatch.setattr(sd, "detect_sd_cards", lambda *a, **k: [])


@pytest.fixture
def window(qapp, isolated_settings):
    from PyQt5.QtCore import QTimer

    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt.main_window import CyberControllerWindow

    bus = EventBus()
    win = CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))
    try:
        win._health.stop()
    except Exception:  # noqa: BLE001
        pass
    for t in win.findChildren(QTimer):
        t.stop()
    yield win
    try:
        win.close()
    except Exception:  # noqa: BLE001
        pass
    win.deleteLater()
    qapp.processEvents()


def test_clear_terminal_clears_the_visible_persistent_terminal(window):
    win = window
    win._pterm_output.append("visible line A")
    win._pterm_output.append("visible line B")
    dev_term = getattr(win._device_tab, "_terminal", None)
    if dev_term is not None:
        dev_term.append("device line")
    assert win._pterm_output.toPlainText().strip() != ""  # precondition

    win._on_clear_terminal()  # the palette slot

    # The bug: the always-visible bottom panel was NOT cleared. It must be now.
    assert win._pterm_output.toPlainText().strip() == ""
    if dev_term is not None:
        assert dev_term.toPlainText().strip() == ""
