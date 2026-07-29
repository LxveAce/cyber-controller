"""The app terminal is all-source: node + target actions now emit to the activity log.

The activity_log bus feeds the main-window terminal (main_window subscribes to its `line` signal).
Many surfaces already emit (flash / crack / broadcast / operate / macro / …), but the Nodes + Target
surfaces did not — a node provision or a target attack fired with no terminal trace. They now emit,
so the terminal reflects every action, not just serial RX. Offscreen Qt.
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


@pytest.fixture()
def activity_lines():
    from src.core.activity_log import activity_log
    seen: list[tuple[str, str, str]] = []
    activity_log().line.connect(lambda s, lvl, t: seen.append((s, lvl, t)))
    return seen


class _FakeNodesCtrl:
    def is_unlocked(self):
        return True

    def list_rows(self):
        return []

    def provision(self, node_id, role="host", label=""):
        pass

    def detach(self, node_id):
        pass


def test_node_actions_emit_to_the_terminal(qapp, activity_lines):
    from src.ui.qt.nodes_tab import NodesTab
    tab = NodesTab(controller=_FakeNodesCtrl())
    tab._do_provision(7, role="host")
    tab._do_detach(7)
    node_lines = [t for s, _lvl, t in activity_lines if s == "nodes"]
    assert any("provisioned node 7" in t for t in node_lines)
    assert any("detached node 7" in t for t in node_lines)


def test_target_attack_emits_to_the_terminal(qapp, monkeypatch, activity_lines):
    import src.config.settings as st
    import src.ui.qt.targets_tab as tt
    from src.core.action_resolver import ActionResolver
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.models.action import ActionCategory, TargetAction
    from src.models.target import Target, TargetType

    bus = EventBus()
    tab = tt.TargetsTab(TargetPool(bus), bus, DeviceManager(), ActionResolver(DeviceManager()))
    monkeypatch.setattr(tt, "_HAS_ACTION_RESOLVER", True)
    monkeypatch.setattr(tt, "_execute_action_fn", lambda *a, **k: True)
    monkeypatch.setattr(st, "load_settings", lambda: {})
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Yes))
    action = TargetAction("Deauth AP", "attack -t deauth", "deauth", ActionCategory.ATTACK)
    target = Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP, ssid="Net",
                    channel=6, rssi=-40, device_source="COM8")
    tab._execute_action(action, "COM8", target)
    assert any(s == "targets" and "Deauth AP" in t for s, _lvl, t in activity_lines)
