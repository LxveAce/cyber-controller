"""SegmentedPills (reform W1) — the bounded second-axis sub-nav: single-select, never overflows.

Replaces the inner QTabWidget strips (whose scroll-arrow overflow is the defect the reform kills):
at most four mutually-exclusive pills, the first auto-selected, a user click emits the key while a
programmatic select does not (so a host can reflect an external nav change with no feedback loop).
Display only — no send path. Offscreen Qt.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt.segmented_pills import SegmentedPills  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_builds_pills_and_auto_selects_first(qapp):
    p = SegmentedPills()
    p.set_segments([("dashboard", "Dashboard"), ("firmware", "Firmware")])
    assert p.keys() == ["dashboard", "firmware"]
    assert p.current() == "dashboard"          # first is selected without emitting


def test_bounded_to_four_never_overflows(qapp):
    p = SegmentedPills()
    p.set_segments([(f"k{i}", f"L{i}") for i in range(6)])
    assert len(p.keys()) == 4       # the reform's <=4 bound; excess dropped, not scrolled


def test_click_emits_and_is_exclusive(qapp):
    p = SegmentedPills()
    p.set_segments([("a", "A"), ("b", "B"), ("c", "C")])
    got: list[str] = []
    p.segment_selected.connect(got.append)
    p._pills[2][1].click()
    assert got == ["c"]                        # user click emits the key
    assert p.current() == "c"                  # and selects it
    assert p._pills[0][1].isChecked() is False  # exclusive: the first is now unchecked


def test_programmatic_select_does_not_emit(qapp):
    p = SegmentedPills()
    p.set_segments([("a", "A"), ("b", "B")])
    got: list[str] = []
    p.segment_selected.connect(got.append)
    p.select("b")
    assert p.current() == "b" and got == []    # reflects an external nav change, no feedback loop
    p.select("nope")                            # unknown key -> no-op, still "b"
    assert p.current() == "b"


def test_rebuild_replaces_old_pills(qapp):
    p = SegmentedPills()
    p.set_segments([("a", "A"), ("b", "B")])
    p.set_segments([("console", "Console"), ("macros", "Macros")])
    assert p.keys() == ["console", "macros"] and p.current() == "console"
