"""Wave-3 adaptive layout: the Devices tab's list/detail split reflows with the window size.

The layout DECISION is unit-tested in test_layout_profile (the pure ``device_layout``); here we
verify the widget APPLIES it — the QSplitter orientation flips vertical<->horizontal — and that the
resize handler tracks the resolver + debounces on the size class. Offscreen Qt; no real device I/O.
The reflow is size-driven only; it must never touch the user's Simple/Pro depth.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core.device_manager import DeviceManager  # noqa: E402
from src.ui.qt import device_tab as DT  # noqa: E402
from src.ui.qt.layout_profile import device_layout, layout_profile  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _expected_orient(tab):
    prof = layout_profile(max(1, tab.width()), max(1, tab.height()), touch=False,
                          dpi=tab.logicalDpiX() or 96)
    return Qt.Vertical if device_layout(prof).stack_panels else Qt.Horizontal


def test_apply_device_layout_flips_orientation(qapp):
    tab = DT.DeviceTab(DeviceManager())
    tab._apply_device_layout(device_layout(layout_profile(480, 800)))    # compact -> vertical
    assert tab._splitter.orientation() == Qt.Vertical
    tab._apply_device_layout(device_layout(layout_profile(1600, 900)))   # expanded -> horizontal
    assert tab._splitter.orientation() == Qt.Horizontal


def test_collapse_chrome_hides_supplementary_but_keeps_safety(qapp):
    # Dense chrome trims the informational detail (caps / telemetry / airspace snapshot) but must
    # never hide the safety-critical arm lamp, detector-alert line, or connection-health line.
    # Offscreen -> use isHidden() (isVisible() is always False without a shown top-level).
    tab = DT.DeviceTab(DeviceManager())
    supp = (tab._caps_label, tab._telemetry_label, tab._snapshot_label)
    safety = (tab._arm_label, tab._alert_label, tab._health_label)

    tab._apply_device_layout(device_layout(layout_profile(480, 800)))    # compact -> dense chrome
    assert all(lbl.isHidden() for lbl in supp), "supplementary detail should hide on dense chrome"
    assert not any(lbl.isHidden() for lbl in safety), "arm/alert/health must stay visible"

    tab._apply_device_layout(device_layout(layout_profile(1600, 900)))   # expanded -> full chrome
    assert not any(lbl.isHidden() for lbl in supp), "supplementary detail restored with room"


def test_relayout_matches_the_resolver(qapp):
    tab = DT.DeviceTab(DeviceManager())
    for w, h in [(400, 800), (1600, 900)]:
        tab.resize(w, h)
        qapp.processEvents()
        tab._relayout_for_size()
        # The applied orientation must match what the resolver says for the tab's ACTUAL width
        # (robust to any minimum-size clamping the widget applies).
        assert tab._splitter.orientation() == _expected_orient(tab)
        assert tab._last_device_size is not None


def test_relayout_debounces_on_size_class(qapp):
    tab = DT.DeviceTab(DeviceManager())
    tab.resize(400, 800)
    qapp.processEvents()
    tab._relayout_for_size()
    first = tab._last_device_size
    # Calling again at the same size class is a no-op — same recorded size, still valid, no error.
    tab._relayout_for_size()
    assert tab._last_device_size == first
    assert tab._splitter.orientation() == _expected_orient(tab)
