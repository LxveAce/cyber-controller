"""Wave-10 Phase C (Phase D polish): the app-shell omnibar is fused with the command palette.

The design brief's omnibar is "command input fused with fuzzy search". Submitting it (Enter) hands
the typed text to the command palette as a pre-filled fuzzy query — the operator then confirms with
Enter, so nothing runs straight off a keystroke (some palette commands are consequential). These
tests assert the wiring (omnibar submit -> palette.open_palette_with(text)) and the fuzzy pre-fill
(prime_query filters + selects the top match), without ever showing the modal exec_ dialog.

Harness mirrors tests/test_command_palette_nav.py (offscreen Qt, real core objects, quiesced).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _no_blocking_sd_probe(monkeypatch):
    import src.core.backends.sd_backend as sd
    monkeypatch.setattr(sd, "detect_sd_cards", lambda *a, **k: [])


def _make_window():
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt.main_window import CyberControllerWindow
    bus = EventBus()
    return CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))


def _quiesce(win) -> None:
    try:
        win._health.stop()
    except Exception:  # noqa: BLE001
        pass
    for timer in win.findChildren(QTimer):
        timer.stop()


@pytest.fixture
def win(qapp):
    w = _make_window()
    _quiesce(w)
    yield w
    try:
        w.close()
    except Exception:  # noqa: BLE001
        pass
    w.deleteLater()
    qapp.processEvents()


def test_omnibar_submit_opens_the_palette_with_the_typed_text(win):
    # Emitting the app-shell omnibar signal must route through the handler into the palette,
    # pre-filled with the typed text. Stub open_palette_with so the modal exec_ never blocks.
    seen = []
    win._palette.open_palette_with = lambda q: seen.append(q)
    win._app_shell.omnibar_submitted.emit("crack lab")
    assert seen == ["crack lab"]   # the exact text reached the palette as a fuzzy query


def test_prime_query_fuzzy_filters_to_the_top_match(win):
    # prime_query pre-fills + fuzzy-filters without showing the dialog; "connect" resolves uniquely
    # to "Connect to Device" as the top (row 0) match.
    count = win._palette.prime_query("connect")
    assert count >= 1
    top = win._palette._list.item(0)
    assert top is not None and top.text() == "Connect to Device"


def test_prime_query_empty_shows_every_command(win):
    # An empty query is not a filter — the palette shows the full command set (nothing hidden).
    total = len(win._palette._commands)
    assert win._palette.prime_query("") == total


def test_prime_query_no_match_selects_nothing_runnable(win):
    # A query that matches nothing yields an empty list, so a subsequent Enter has no command to run
    # (the safety property: the omnibar can't fire a command that doesn't exist).
    assert win._palette.prime_query("zzzznomatchzzzz") == 0
    assert win._palette._list.currentItem() is None
