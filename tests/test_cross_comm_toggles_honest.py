"""The Cross-Communication settings must not ship inert toggles.

'Auto-share discoveries' and 'De-duplicate targets by MAC' were checkboxes that nothing consumed —
unchecking either changed nothing. They are removed in favor of an honest always-on description. This
guards against a regression that re-adds a toggle promising to turn off behavior that is actually fixed,
and pins the real always-on behavior: TargetPool always de-duplicates by MAC.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_pool_always_dedups_by_mac():
    from src.core.cross_comm import TargetPool, EventBus
    from src.models.target import Target, TargetType
    pool = TargetPool(EventBus())
    assert pool.add(Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP, device_source="COM3")) is True
    # Same MAC + type from any source updates the existing entry rather than adding a second — dedup is
    # intrinsic to the pool key (type:mac), not a toggle.
    assert pool.add(Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP, device_source="COM9")) is False
    assert len(pool.all()) == 1


def test_settings_tab_has_no_inert_crosscomm_toggles(qapp):
    from src.ui.qt.settings_tab import SettingsTab
    tab = SettingsTab()
    assert not hasattr(tab, "_auto_share_check"), "the inert Auto-share toggle must not exist"
    assert not hasattr(tab, "_dedup_check"), "the inert De-dup toggle must not exist"
    # The card itself remains (interface-mode hiding relies on it) but carries no consumed toggle key.
    assert getattr(tab, "_comm_card", None) is not None
    assert "cross_comm" not in tab._gather()
