"""The Recon/Offense posture is a VISIBLE display state — it gates nothing (owner, 2026-07-29).

CC is universally usable out of the box: offensive verbs are reachable by default, gated only by the
first-run authorized-use consent + the per-command pre-execution confirm (safety.py, the untouched
floor). An earlier build made Recon hard-block offensive verbs; that forced-switch was removed. The
tests pin what remains: the posture is a display indicator the shell mirrors into the state module,
with no usage-gate. Offscreen Qt; the posture is a process global, so each test resets.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core import posture as P  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _reset_posture():
    P.set_posture(P.POSTURE_RECON)
    yield
    P.set_posture(P.POSTURE_RECON)


def test_default_is_recon_and_set_get_round_trips():
    assert P.get_posture() == P.POSTURE_RECON
    P.set_posture(P.POSTURE_OFFENSE)
    assert P.get_posture() == P.POSTURE_OFFENSE


def test_bogus_posture_is_ignored():
    P.set_posture(P.POSTURE_OFFENSE)
    P.set_posture("nonsense")
    assert P.get_posture() == P.POSTURE_OFFENSE     # unchanged


def test_posture_exposes_no_usage_gate():
    # The forced Recon-blocks-offensive gate was removed — the module must not re-grow a blocker.
    assert not hasattr(P, "offensive_blocked")
    assert not hasattr(P, "block_reason")


def test_page_layout_constants_do_not_drift_from_core():
    from src.ui.qt.page_layout import POSTURE_OFFENSE, POSTURE_RECON
    assert (POSTURE_RECON, POSTURE_OFFENSE) == (P.POSTURE_RECON, P.POSTURE_OFFENSE)


def test_binder_mirrors_the_visible_posture_into_the_state(qapp):
    from src.ui.qt.page_layout import PageLayout
    from src.ui.qt.page_layout_binder import PageLayoutBinder
    layout = PageLayout()
    PageLayoutBinder(layout, hub=None)
    assert P.get_posture() == P.POSTURE_RECON           # initial sync
    layout.set_posture(P.POSTURE_OFFENSE)                # emits posture_changed -> state follows
    assert P.get_posture() == P.POSTURE_OFFENSE
    layout.set_posture(P.POSTURE_RECON)
    assert P.get_posture() == P.POSTURE_RECON
