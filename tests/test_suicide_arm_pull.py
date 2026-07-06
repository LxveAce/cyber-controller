"""Both armed-level choices in the Dead Man's Switch dialog must yield a FAIL-SAFE (arm_level, arm_pull)
pair — the pin idles NOT-armed. The dialog doesn't expose arm_pull and its default (2/pulldown) is only
fail-safe for HIGH-armed, so picking "LOW = armed" used to always fail provisioning validation.

Fail-safe pairs (provision.py / SPEC 4.1): level=1 + pulldown(2), and level=0 + pullup(1).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

_FAILSAFE = {(1, 2), (0, 1)}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _cfg_for_index(qapp, idx):
    from src.ui.qt.suicide_dialog import SuicideSetupDialog

    d = SuicideSetupDialog()
    d.arm_level.setCurrentIndex(idx)
    return d._collect_cfg()


def test_high_armed_pair_is_failsafe(qapp):
    cfg = _cfg_for_index(qapp, 0)  # "HIGH = armed (1)"
    assert (cfg.arm_level, cfg.arm_pull) == (1, 2)
    assert (cfg.arm_level, cfg.arm_pull) in _FAILSAFE


def test_low_armed_pair_is_failsafe(qapp):
    cfg = _cfg_for_index(qapp, 1)  # "LOW = armed (0)" — the case that used to always fail
    assert (cfg.arm_level, cfg.arm_pull) == (0, 1)
    assert (cfg.arm_level, cfg.arm_pull) in _FAILSAFE
