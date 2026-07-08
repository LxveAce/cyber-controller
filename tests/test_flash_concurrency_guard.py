"""The Flash tab must not run two esptool flashes at once (single + batch).

Regression for audit finding [2]: during a single flash, ``_btn_flash_queue`` stayed enabled and
``_on_flash_queue``'s guard checked only the batch worker — never the single-flash ``_worker`` — so clicking
"Flash Queue" mid-flash started a concurrent batch. (The engine's per-port lock stops an actual same-port
brick, but the UI let the confusing double-start happen; the reverse direction was already guarded.)
Offscreen Qt; fixture pattern mirrors test_terminal_clear.py.
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
    import src.core.backends.sd_backend as sd
    monkeypatch.setattr(sd, "detect_sd_cards", lambda *a, **k: [])


@pytest.fixture
def flash_tab(qapp, isolated_settings):
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
    yield win._flash_tab
    try:
        win.close()
    except Exception:  # noqa: BLE001
        pass
    win.deleteLater()
    qapp.processEvents()


class _FakeRunning:
    """Stand-in for a live _FlashWorker QThread."""

    def isRunning(self):  # noqa: N802 (Qt naming)
        return True


def test_flash_queue_refused_while_single_flash_running(flash_tab):
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QListWidgetItem

    ft = flash_tab
    ft._worker = _FakeRunning()
    ft._batch_worker = None
    ft._batch_jobs = []
    # Queue one job with an unknown profile so the OLD (unguarded) path runs to completion WITHOUT spawning
    # a real flash worker (it skips the unknown profile) — keeps this test hardware/network-free either way.
    item = QListWidgetItem("COM_TEST -> __no_such_profile__")
    item.setData(Qt.UserRole, ("COM_TEST", "__no_such_profile__"))
    ft._queue_list.addItem(item)
    ft._log_output.clear()

    ft._on_flash_queue()

    log = ft._log_output.toPlainText()
    assert "already in progress" in log        # the single-flash guard fired
    assert "Batch: flashing" not in log        # a batch was NOT started


def test_on_flash_reentry_guarded_while_worker_running(flash_tab):
    ft = flash_tab
    fake = _FakeRunning()
    ft._worker = fake
    ft._batch_worker = None
    ft._log_output.clear()

    ft._on_flash()

    assert ft._worker is fake                                     # no new worker spawned/overwritten
    assert "already in progress" in ft._log_output.toPlainText()  # the re-entry guard fired


def test_flash_done_reenables_both_flash_buttons(flash_tab):
    ft = flash_tab
    ft._btn_flash.setEnabled(False)
    ft._btn_flash_queue.setEnabled(False)

    ft._on_flash_done(True)

    assert ft._btn_flash.isEnabled()
    assert ft._btn_flash_queue.isEnabled()   # was left disabled before the fix
