"""The Flash tab 'Batch Queue' must actually flash the queued jobs.

Regression: 'Add to Queue' / 'Clear Queue' were wired but nothing flashed the queue — there was no run
handler and _queue_list was never iterated, so a documented feature (the in-app How-To promises 'Queue
multiple port+profile combos and flash them sequentially') collected entries nothing consumed. A 'Flash
Queue' button now walks the queue and flashes each entry sequentially on the proven single-flash path.

Offscreen Qt; the flash worker is stubbed so no real port/network is touched.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QListWidgetItem  # noqa: E402
from PyQt5.QtCore import Qt  # noqa: E402

from src.core.device_manager import DeviceManager  # noqa: E402
from src.core.flash_engine import FlashEngine  # noqa: E402
from src.core.resources import resource_path  # noqa: E402
from src.ui.qt import flash_tab as FT  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    import src.config.settings as S
    monkeypatch.setattr(S, "SETTINGS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(S, "SETTINGS_PATH", tmp_path / "settings.json", raising=False)


def _queue(tab, *entries):
    for port, profile in entries:
        it = QListWidgetItem(f"{port} -> {profile}")
        it.setData(Qt.UserRole, (port, profile))
        tab._queue_list.addItem(it)


def _stub_worker(monkeypatch, flashed):
    real_init = FT._FlashWorker.__init__

    def spy_init(self, engine, port, profile, *a, **k):
        flashed.append(port)
        real_init(self, engine, port, profile, *a, **k)

    monkeypatch.setattr(FT._FlashWorker, "__init__", spy_init)
    # Emit the finished signal synchronously so the sequential chain advances without a real flash.
    monkeypatch.setattr(FT._FlashWorker, "start", lambda self: self.finished.emit(True))


def test_flash_queue_flashes_every_queued_job(qapp, isolated_settings, monkeypatch):
    tab = FT.FlashTab(DeviceManager(), FlashEngine())
    tab._profiles["marauder"] = resource_path("src", "config", "profiles", "marauder.json")
    _queue(tab, ("COM3", "marauder"), ("COM4", "marauder"))

    flashed: list[str] = []
    _stub_worker(monkeypatch, flashed)

    tab._on_flash_queue()

    assert flashed == ["COM3", "COM4"], "every queued (port, profile) must be flashed, in order"
    assert tab._btn_flash_queue.isEnabled(), "the Flash Queue button must re-enable when the batch ends"
    assert tab._batch_jobs == [], "batch state must reset after completion"


def test_empty_queue_flashes_nothing(qapp, isolated_settings, monkeypatch):
    tab = FT.FlashTab(DeviceManager(), FlashEngine())
    flashed: list[str] = []
    _stub_worker(monkeypatch, flashed)

    tab._on_flash_queue()

    assert flashed == [], "an empty queue must construct no flash worker"
