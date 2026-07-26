"""Shared PageLayout frame (src/ui/qt/page_layout.py) — the GUI-rebuild shell, offscreen.

Asserts the CLAIM structurally: the one frame every screen inherits really exposes its slots
(destinations + count badges, device-truth status fields, a posture toggle boundary, an omnibar) and
its collapse behaviour — the reusable primitive Phase C re-parents the app into. No live hub here;
this is the additive component in isolation.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

from src.ui.qt.page_layout import (  # noqa: E402
    POSTURE_OFFENSE,
    POSTURE_RECON,
    PageLayout,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_frame_builds_with_empty_slots(qapp):
    p = PageLayout()
    assert p.posture == POSTURE_RECON
    assert p.collapsed is False
    assert p._destinations == {}


def test_destinations_select_and_emit(qapp):
    p = PageLayout()
    p.add_destination("wifi", "Wi-Fi", "📶")
    p.add_destination("ble", "BLE", "📱")
    got = []
    p.destination_selected.connect(got.append)
    p.select_destination("ble")
    assert got == ["ble"]
    # selecting one checks it, unchecks the others (single active destination)
    assert p._destinations["ble"].isChecked() and not p._destinations["wifi"].isChecked()
    # a duplicate add is a no-op (stable identity)
    p.add_destination("wifi", "Wi-Fi again")
    assert len(p._destinations) == 2


def test_count_badge_shows_and_hides(qapp):
    p = PageLayout()
    p.add_destination("captures", "Captures")
    p.set_badge("captures", 3)
    assert "(3)" in p._destinations["captures"].text()
    p.set_badge("captures", 0)          # zero hides the badge, not a "(0)"
    assert "(0)" not in p._destinations["captures"].text()
    p.set_badge("nope", 5)              # unknown destination is a safe no-op


def test_status_fields_set_and_hide(qapp):
    p = PageLayout()
    # isHidden() reflects the explicit visibility flag (isVisible() needs the window shown,
    # which never happens in an offscreen test).
    p.set_status("link", "LoRa", color=None)
    assert p._status["link"].text() == "LoRa" and not p._status["link"].isHidden()
    p.set_status("link", "")            # empty text hides the field
    assert p._status["link"].isHidden()
    p.set_status("bogus", "x")          # unknown field is a safe no-op


def test_posture_toggle_is_a_boundary_signal(qapp):
    p = PageLayout()
    seen = []
    p.posture_changed.connect(seen.append)
    p._on_posture_clicked()             # simulate the header toggle click
    assert p.posture == POSTURE_OFFENSE and seen == [POSTURE_OFFENSE]
    p._on_posture_clicked()
    assert p.posture == POSTURE_RECON and seen == [POSTURE_OFFENSE, POSTURE_RECON]
    # set_posture is idempotent (no duplicate emit for the same state)
    p.set_posture(POSTURE_RECON)
    assert seen == [POSTURE_OFFENSE, POSTURE_RECON]


def test_sidebar_collapses(qapp):
    p = PageLayout()
    p.add_destination("wifi", "Wi-Fi", "📶")
    assert p.collapsed is False
    full = p._destinations["wifi"].text()
    p.toggle_sidebar()
    assert p.collapsed is True
    # collapsed rail hides the label text (icon-only), and narrows the rail
    assert p._destinations["wifi"].text() != full
    assert p._sidebar.maximumWidth() <= 44
    p.toggle_sidebar()
    assert p.collapsed is False


def test_set_content_replaces(qapp):
    p = PageLayout()
    a, b = QLabel("A"), QLabel("B")
    p.set_content(a)
    assert p._content is a
    p.set_content(b)
    assert p._content is b and a.parent() is None   # old content detached


def test_omnibar_emits_nonempty(qapp):
    p = PageLayout()
    seen = []
    p.omnibar_submitted.connect(seen.append)
    p._omnibar.setText("  scan wifi  ")
    p._on_omnibar()
    assert seen == ["scan wifi"]        # trimmed
    p._omnibar.setText("   ")
    p._on_omnibar()
    assert seen == ["scan wifi"]        # blank does not emit


def test_content_lives_in_the_frame_not_a_separate_window(qapp):
    # The frame is one widget tree: content + sidebar + status bar share the PageLayout parent, so a
    # host embeds ONE thing. Guards against accidentally spawning a detached window.
    p = PageLayout()
    c = QWidget()
    p.set_content(c)
    assert c.window() is p.window()
