"""Concurrency / teardown hardening for the Qt UI (subsystem: ui-qt-core).

Regression coverage for three confirmed defects:

  #1 FlashTab's background worker QThreads (variant loader / flash / detect / vault / backup-erase)
     were never join()ed. A tab is a child widget, so it never gets its own closeEvent when the main
     window closes — the still-running QThread was then GC'd with the window and its C++ dtor aborted
     the process ('QThread: Destroyed while thread is still running'), most visible in the frozen build.
     Fix: FlashTab.shutdown() waits every in-flight worker; main_window.closeEvent calls it.

  #2 main_window.closeEvent never waited on the update / self-update check QThreads (started on launch
     and on a manual check) — same GC-abort on exit. Fix: closeEvent joins them.

  #3 A serial port co-owned by BOTH the persistent terminal and the Devices tab double-processed a
     Dead Man's Switch auth prompt: one received line fired both on_line callbacks, so check_line ran
     twice -> a second modal password dialog stacked on the first and the boot password was written to
     the gate TWICE (a wrong/extra attempt the DMS can read as tamper -> wipe/brick). Fix: the Devices
     tab is the sole DMS owner for any port it has connected; the terminal defers on those ports.

Offscreen; builds a real CyberControllerWindow so the wiring under test is exercised end-to-end.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import types

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    import src.config.settings as S
    monkeypatch.setattr(S, "SETTINGS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(S, "SETTINGS_PATH", tmp_path / "settings.json", raising=False)
    return S


@pytest.fixture(autouse=True)
def _fast_offline_window(monkeypatch):
    """Keep window construction cheap and offline. FlashTab auto-starts a _VariantLoader that would hit
    the network, and SoftwareTab.__init__ shells out to PowerShell for SD detection (up to a 15s
    subprocess on Windows). Neither is under test here — stub both to instant, empty results."""
    import src.core.backends.sd_backend as sd
    monkeypatch.setattr(sd, "detect_sd_cards", lambda *a, **k: [], raising=False)
    from src.core.flash_engine import FlashEngine
    monkeypatch.setattr(FlashEngine, "list_variants", lambda self, profile: [], raising=False)


def _make_window():
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt.main_window import CyberControllerWindow

    bus = EventBus()
    return CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))


class _FakeWorker:
    """Stand-in for an in-flight worker QThread: reports itself running until wait() is called, and
    records the timeout it was joined with so a test can prove the teardown path waited on it."""

    def __init__(self):
        self._running = True
        self.wait_ms = None

    def isRunning(self):
        return self._running

    def wait(self, ms=None):
        self.wait_ms = ms
        self._running = False
        return True


# ── #1 FlashTab worker teardown ──────────────────────────────────────────────

def test_closeevent_joins_flashtab_bg_workers(qapp, isolated_settings):
    # A worker sitting in FlashTab._bg_workers (variant loader / vault / backup-erase) must be joined
    # when the main window closes, not left running to abort the process on teardown.
    win = _make_window()
    worker = _FakeWorker()
    win._flash_tab._bg_workers.add(worker)
    win.close()  # runs the real closeEvent
    assert worker.wait_ms is not None, "closeEvent did not wait on the FlashTab background worker"
    assert worker.isRunning() is False


def test_flashtab_shutdown_joins_flash_detect_op_workers(qapp, isolated_settings):
    # _FlashWorker / _DetectWorker / _OpWorker live on their own attributes (the first two are NOT in
    # _bg_workers) — shutdown() must join those too.
    win = _make_window()
    ft = win._flash_tab
    flash_w, detect_w, op_w = _FakeWorker(), _FakeWorker(), _FakeWorker()
    ft._worker = flash_w
    ft._detect_worker = detect_w
    ft._op_worker = op_w
    try:
        ft.shutdown()
        assert flash_w.wait_ms is not None, "shutdown did not wait on the flash worker"
        assert detect_w.wait_ms is not None, "shutdown did not wait on the detect worker"
        assert op_w.wait_ms is not None, "shutdown did not wait on the op worker"
    finally:
        win.close()


# ── #2 update / self-update worker teardown ──────────────────────────────────

def test_closeevent_joins_update_workers(qapp, isolated_settings):
    win = _make_window()
    update_w, self_update_w = _FakeWorker(), _FakeWorker()
    win._update_worker = update_w
    win._self_update_worker = self_update_w
    win.close()
    assert update_w.wait_ms is not None, "closeEvent did not wait on the update-check worker"
    assert self_update_w.wait_ms is not None, "closeEvent did not wait on the self-update worker"


# ── #3 DMS single-owner on a co-owned port ───────────────────────────────────

def test_dms_single_check_line_across_coowned_callbacks(qapp, isolated_settings, monkeypatch):
    # One physical serial line delivered to BOTH co-owner callbacks (terminal + Devices tab) must be
    # run through DeadManAuth exactly ONCE — not twice (which stacks a second password dialog and
    # writes the boot password to the gate a second time).
    win = _make_window()
    try:
        conn = types.SimpleNamespace(is_connected=True, write=lambda *_a: None)
        win._pterm_conns["COM7"] = conn
        # Devices tab co-owns COM7: it registered its own on_line callback for this port.
        win._device_tab._devtab_line_cbs["COM7"] = lambda *_a: None
        # DeviceTab._on_line_received resolves the emitting connection via get_connection.
        monkeypatch.setattr(win._dm, "get_connection", lambda port: conn if port == "COM7" else None)

        calls = []
        # Same shared DeadManAuth instance backs both handlers (main window assigns it to the device tab).
        win._dms_auth.check_line = lambda line, send_fn: (calls.append(line), False)[1]

        line = "suicide-gate: enter 'unlock <password>'"
        win._pterm_on_line("COM7", line)            # terminal callback
        win._device_tab._on_line_received("COM7", line)  # devices-tab callback (same physical line)

        assert calls == [line], f"DMS prompt was processed {len(calls)}x on a co-owned port (want 1x)"
    finally:
        win.close()


def test_dms_pterm_still_handles_port_it_owns_alone(qapp, isolated_settings):
    # When the Devices tab is NOT connected on the port, the terminal must remain the DMS handler —
    # the de-dup must not silently drop the prompt (never answering the gate burns attempts -> brick).
    win = _make_window()
    try:
        conn = types.SimpleNamespace(is_connected=True, write=lambda *_a: None)
        win._pterm_conns["COM7"] = conn
        win._device_tab._devtab_line_cbs.pop("COM7", None)  # devices tab not co-owning COM7

        calls = []
        win._dms_auth.check_line = lambda line, send_fn: (calls.append(line), False)[1]

        line = "suicide-gate: enter 'unlock <password>'"
        win._pterm_on_line("COM7", line)
        assert calls == [line], "terminal must handle DMS on a port it owns alone"
    finally:
        win.close()
