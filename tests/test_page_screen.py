"""PageScreen scaffold (WS4 F1) — the shared master/detail/actions container reflows responsively.

Every WS4 screen mounts into this one scaffold (master | detail | actions) so they share structure
instead of ad-hoc inner chrome. These assert the region API (hold/swap/clear), the primary-action
hook the host hoists into the frame's docked bar, and the responsive reflow: side-by-side on
regular/expanded, a vertical stack on compact, with the actions region folding on the mid width.
Display only — the scaffold has no send path. Offscreen Qt.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QLabel  # noqa: E402

from src.ui.qt.layout_profile import layout_profile  # noqa: E402
from src.ui.qt.page_screen import PageScreen  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_regions_hold_and_swap_content(qapp):
    s = PageScreen("wifi")
    m, d, a = QLabel("master"), QLabel("detail"), QLabel("actions")
    s.set_master(m)
    s.set_detail(d)
    s.set_actions(a)
    assert m.parent() is s._master_holder
    assert d.parent() is s._detail_holder
    assert a.parent() is s._actions_holder
    # swapping replaces (the old widget is reparented out of the region)
    m2 = QLabel("master2")
    s.set_master(m2)
    assert s._master_lay.count() == 1 and s._master_lay.itemAt(0).widget() is m2


def test_clearing_a_region_with_none(qapp):
    s = PageScreen("wifi")
    s.set_detail(QLabel("x"))
    s.set_detail(None)
    assert s._detail_lay.count() == 0


def test_primary_action_hook(qapp):
    s = PageScreen("wifi")
    assert s.primary_action() is None            # nothing declared yet
    btn = QLabel("GO")
    s.set_primary_action(btn)
    assert s.primary_action() is btn             # the host hoists this into the frame's action bar


def test_relayout_reflows_orientation_and_actions(qapp):
    s = PageScreen("wifi")
    s.relayout(layout_profile(1440, 900))        # expanded: side-by-side, actions inline
    assert s._splitter.orientation() == Qt.Horizontal
    assert not s._actions_holder.isHidden()
    s.relayout(layout_profile(800, 700))         # regular: side-by-side, actions FOLD
    assert s._splitter.orientation() == Qt.Horizontal
    assert s._actions_holder.isHidden()
    s.relayout(layout_profile(400, 800))         # compact: vertical STACK, actions reachable again
    assert s._splitter.orientation() == Qt.Vertical
    assert not s._actions_holder.isHidden()
    assert s._last_size == "compact"
