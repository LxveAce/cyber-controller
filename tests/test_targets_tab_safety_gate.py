"""The Targets-tab right-click attack actions must clear the 'Confirm before dangerous commands' gate.

Regression: TargetsTab._execute_action sent the deauth/beacon command straight to the radio with no
safety.classify / should_confirm check, so the marketed safety toggle was inert on this one surface while
every sibling send surface (device_tab, network_tab) gated it. Offscreen Qt.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QMessageBox  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_tab():
    from src.core.cross_comm import TargetPool, EventBus
    from src.core.action_resolver import ActionResolver
    from src.core.device_manager import DeviceManager
    from src.ui.qt.targets_tab import TargetsTab

    dm = DeviceManager()
    bus = EventBus()
    pool = TargetPool(bus)
    return TargetsTab(pool, bus, dm, ActionResolver(dm))


def _attack_action():
    from src.models.action import TargetAction, ActionCategory
    return TargetAction("Deauth AP", "attack -t deauth", "deauth", ActionCategory.ATTACK,
                        pre_commands=["select -a 0"])


def _target():
    from src.models.target import Target, TargetType
    return Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP, ssid="Net",
                  channel=6, rssi=-40, device_source="COM8")


def _patch_common(monkeypatch, tab, sent):
    import src.ui.qt.targets_tab as tt
    import src.config.settings as st
    monkeypatch.setattr(tt, "_HAS_ACTION_RESOLVER", True)
    monkeypatch.setattr(tt, "_execute_action_fn",
                        lambda *a, **k: sent.append(a) or True)
    monkeypatch.setattr(st, "load_settings", lambda: {})  # default -> confirm_dangerous True


def test_attack_action_blocked_when_confirmation_declined(qapp, monkeypatch):
    tab = _make_tab()
    sent: list = []
    _patch_common(monkeypatch, tab, sent)
    warned: list = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: warned.append(a) or QMessageBox.No)
    tab._execute_action(_attack_action(), "COM8", _target())
    assert warned, "dangerous action must pop the confirm dialog"
    assert sent == [], "declining the confirm must NOT send the attack command"


def test_attack_action_sent_when_confirmed(qapp, monkeypatch):
    tab = _make_tab()
    sent: list = []
    _patch_common(monkeypatch, tab, sent)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)
    tab._execute_action(_attack_action(), "COM8", _target())
    assert len(sent) == 1, "confirming the gate must send the attack command exactly once"


def test_safe_action_not_gated(qapp, monkeypatch):
    from src.models.action import TargetAction, ActionCategory
    tab = _make_tab()
    sent: list = []
    _patch_common(monkeypatch, tab, sent)
    warned: list = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: warned.append(a) or QMessageBox.No)
    safe = TargetAction("Monitor Channel", "setch 6", "monitor", ActionCategory.MONITOR)
    tab._execute_action(safe, "COM8", _target())
    assert warned == [], "a safe monitor action must not trigger the dangerous-command confirm"
    assert len(sent) == 1, "a safe action still executes"
