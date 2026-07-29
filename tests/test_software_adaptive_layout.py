"""Wave-3 (bespoke): the Software-OS tab reflows AND its destructive write stays confirm-gated.

Two things. (1) The OS-picker / target-USB / action columns stack on a compact canvas instead of
cramping three cards across a narrow deck. (2) A safety regression-lock: the whole-disk OS write
(which ERASES the USB) must not start unless the user accepts the "Erase and flash?" confirm — the
owner's model is a pre-execution confirm (not a posture gate). Offscreen Qt.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QBoxLayout, QMessageBox  # noqa: E402

from src.ui.qt import touch_mode as TM  # noqa: E402
from src.ui.qt.layout_profile import layout_profile  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _reset_touch():
    TM.set_touch_mode("off")
    yield
    TM.set_touch_mode("auto")


def _tab():
    from src.ui.qt.software_tab import SoftwareTab
    return SoftwareTab()


def test_top_row_stacks_on_compact(qapp):
    tab = _tab()
    tab.resize(480, 800)
    qapp.processEvents()
    tab._last_sw_size = None
    tab._relayout_software()
    assert tab._top_row.direction() == QBoxLayout.TopToBottom
    tab.resize(1200, 800)
    qapp.processEvents()
    tab._relayout_software()
    assert tab._top_row.direction() == QBoxLayout.LeftToRight


def _arm_flashable(tab):
    """Put the tab where _on_flash reaches the confirm: entry + resolved + a selected drive."""
    tab._current_entry = lambda: SimpleNamespace(name="Kali", id="kali")   # type: ignore[assignment]
    tab._resolved = SimpleNamespace(version="2025.1")
    tab._drive_combo.addItem("USB (8 GB removable)", "/dev/fakeusb")
    tab._drive_combo.setCurrentIndex(tab._drive_combo.count() - 1)


class _StubSignal:
    def connect(self, *_a, **_k):
        pass


class _StubWorker:
    started = []

    def __init__(self, *a, **k):
        self.progress = _StubSignal()
        self.finished = _StubSignal()

    def start(self):
        _StubWorker.started.append(True)


def test_destructive_write_does_not_start_when_confirm_declined(qapp, monkeypatch):
    import src.ui.qt.software_tab as ST
    tab = _tab()
    _arm_flashable(tab)
    _StubWorker.started = []
    monkeypatch.setattr(ST, "_OSFlashWorker", _StubWorker)
    monkeypatch.setattr(ST.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: QMessageBox.No))
    tab._on_flash()
    assert _StubWorker.started == []           # DECLINED -> the erase+write never begins
    assert tab._worker is None


def test_destructive_write_starts_only_after_confirm_accepted(qapp, monkeypatch):
    import src.ui.qt.software_tab as ST
    tab = _tab()
    _arm_flashable(tab)
    _StubWorker.started = []
    monkeypatch.setattr(ST, "_OSFlashWorker", _StubWorker)
    monkeypatch.setattr(ST.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    tab._on_flash()
    assert _StubWorker.started == [True]       # ACCEPTED -> the write starts (stub, no real IO)


def test_relayout_matches_the_resolver_and_debounces(qapp):
    tab = _tab()
    for w in (400, 1400):
        tab.resize(w, 800)
        qapp.processEvents()
        tab._relayout_software()
        p = layout_profile(max(1, tab.width()), max(1, tab.height()), touch=False,
                           dpi=tab.logicalDpiX() or 96)
        expected = QBoxLayout.TopToBottom if p.is_compact else QBoxLayout.LeftToRight
        assert tab._top_row.direction() == expected
    first = tab._last_sw_size
    tab._relayout_software()   # same size class -> no-op
    assert tab._last_sw_size == first
