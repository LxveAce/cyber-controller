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


class _FakeSignal:
    """A minimal signal stand-in: records connected slots and fires them on emit()."""

    def __init__(self) -> None:
        self._slots: list = []

    def connect(self, fn) -> None:
        self._slots.append(fn)

    def emit(self, *args) -> None:
        for fn in list(self._slots):
            fn(*args)


class _FakeResolver:
    """Stand-in for _ResolveWorker with a controllable running state — no real QThread/network."""

    def __init__(self, entry, offline) -> None:
        self.entry = entry
        self.offline = offline
        self._running = False
        self.done = _FakeSignal()
        self.finished = _FakeSignal()

    def start(self) -> None:
        self._running = True

    def isRunning(self) -> bool:  # noqa: N802 (Qt API name)
        return self._running


def test_flash_double_click_does_not_orphan_inflight_resolver(qapp, monkeypatch):
    """Regression for the double-click 'Flash OS' orphan: a second entry while a resolve is still running
    must NOT reassign self._resolver (which would GC-destroy the first, still-running QThread and abort)."""
    from src.ui.qt import software_tab
    monkeypatch.setattr(software_tab.sd, "detect_sd_cards",
                        lambda *_a, **_k: [{"device": r"\\.\PhysicalDrive9", "name": "USB", "size": 16 << 30}])
    monkeypatch.setattr(software_tab, "_ResolveWorker", _FakeResolver)
    tab = software_tab.SoftwareTab()

    # First 'Flash OS' click with nothing resolved kicks off an auto-resolve and disables the flash button.
    assert tab._resolved is None
    tab._on_flash()
    first = tab._resolver
    assert isinstance(first, _FakeResolver) and first.isRunning()
    assert not tab._btn_flash.isEnabled(), "Flash OS must be disabled while the auto-resolve is pending"

    # The common reaction — click 'Flash OS' / 'Check latest' again before it finishes — must be a no-op
    # on the worker, not a reassignment that drops the running thread.
    tab._on_check()
    assert tab._resolver is first, "in-flight resolve QThread was orphaned by a re-entrant check"

    # When it finishes, buttons re-enable and the reference is cleared so a fresh resolve is allowed.
    first.done.emit(None, "")
    assert tab._btn_flash.isEnabled()
    first._running = False
    first.finished.emit()  # QThread.finished — clears the reference post-run
    assert tab._resolver is None
    tab._on_check()
    assert tab._resolver is not None and tab._resolver is not first


def test_shutdown_waits_for_running_workers(qapp, monkeypatch):
    """closeEvent calls SoftwareTab.shutdown(); it must join both the resolve and OS-flash QThreads so
    neither is destroyed mid-run (aborting the process / cutting off a destructive USB write)."""
    from src.ui.qt import software_tab
    monkeypatch.setattr(software_tab.sd, "detect_sd_cards", lambda *_a, **_k: [])
    tab = software_tab.SoftwareTab()

    class _W:
        def __init__(self) -> None:
            self.waited = False
            self._run = True

        def isRunning(self) -> bool:  # noqa: N802
            return self._run

        def wait(self, *_a) -> bool:
            self.waited = True
            self._run = False
            return True

    resolver, worker = _W(), _W()
    tab._resolver = resolver
    tab._worker = worker
    tab.shutdown()
    assert resolver.waited, "shutdown must wait for the in-flight resolve worker"
    assert worker.waited, "shutdown must wait for the running OS-flash worker"
