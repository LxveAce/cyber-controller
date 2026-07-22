"""A5 #18 — the command palette can jump to every grouped sub-view.

Before this, the palette could reach Flash / Devices / Health / Targets / Broadcast / Network Graph /
Cross-Comm but NOT the four sub-views a user is just as likely to hunt for — Control (single-device
deep control), BLE Analyzer, Crack Lab, and Manage Nodes. Each was buried one surface-tab click deep
with no keyboard route. This locks in a palette entry for each, and that firing it actually focuses the
right surface *and* the right sub-tab (the two-step _show_subtab contract).

Harness mirrors tests/test_dual_depth_ui.py::_make_window (offscreen Qt, real core objects, quiesced).
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
    """SoftwareTab.__init__ shells out to PowerShell Get-Disk on Windows; stub it so building the
    window is instant and offline (test isolation only — these tests never touch SD detection)."""
    import src.core.backends.sd_backend as sd
    monkeypatch.setattr(sd, "detect_sd_cards", lambda *a, **k: [])


def _make_window():
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.core.cross_comm import EventBus, TargetPool
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
    except Exception:  # noqa: BLE001 — teardown must never raise
        pass
    w.deleteLater()
    qapp.processEvents()


def _run_palette_command(win, label: str) -> None:
    """Invoke the palette command with *label* exactly as selecting it would."""
    matches = [c for c in win._palette._commands if c.label == label]
    assert matches, f"no palette command labelled {label!r}"
    assert len(matches) == 1, f"duplicate palette command {label!r}"
    matches[0].callback()


# (palette label, surface attr, sub-tab attr) — the four sub-views A5 #18 added a route for.
NAV_CASES = [
    ("Control Device", "_operate_surface", "_operate_console"),
    ("Manage Nodes", "_connect_surface", "_nodes_tab"),
    ("Crack Lab", "_network_surface", "_crack_lab_tab"),
    ("BLE Analyzer", "_network_surface", "_ble_analyzer"),
]


@pytest.mark.parametrize("label,surface_attr,sub_attr", NAV_CASES)
def test_palette_navigates_to_subview(win, label, surface_attr, sub_attr):
    surface = getattr(win, surface_attr)
    sub = getattr(win, sub_attr)
    if sub is None:
        pytest.skip(f"{sub_attr} not built in this config")  # BLE analyzer is optional

    # Start somewhere else so the assertion proves the jump, not a coincidental starting position.
    win._tabs.setCurrentWidget(win._settings_tab)
    _run_palette_command(win, label)

    assert win._tabs.currentWidget() is surface        # focused the right top-level surface
    assert surface.currentWidget() is sub              # …and the right sub-tab within it


def test_ble_palette_entry_matches_widget_presence(win):
    """The BLE Analyzer entry is registered iff the analyzer widget exists — never a dead command that
    points at a None sub-tab (the guard in _wire_command_palette)."""
    has_entry = any(c.label == "BLE Analyzer" for c in win._palette._commands)
    assert has_entry == (win._ble_analyzer is not None)
