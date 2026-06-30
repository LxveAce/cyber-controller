"""Network graph (experimental "test" tab) — node/edge build from devices+targets, the per-node command
menus actually route, and a Rebuild preserves the layout the user dragged. Offscreen."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_tab(with_data: bool = True):
    from src.core.device_manager import DeviceManager
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.action_resolver import ActionResolver
    from src.models.device import Device
    from src.models.target import Target, TargetType
    from src.ui.qt.network_tab import NetworkTab

    dm = DeviceManager()
    pool = TargetPool(EventBus())
    sent: "list[tuple[str, str]]" = []
    if with_data:
        dm.add_device(Device(port="COM7", name="Marauder", firmware="marauder", connected=True))
        pool.add(Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP, ssid="HomeNet",
                        channel=6, rssi=-40, device_source="COM7"))
    tab = NetworkTab(dm, pool, ActionResolver(dm), lambda port, cmd: sent.append((port, cmd)))
    return tab, sent


def test_empty_shows_placeholder(qapp):
    tab, _sent = _make_tab(with_data=False)
    assert "_placeholder" in tab._nodes
    assert len(tab._nodes) == 1


def test_builds_device_and_target_nodes_with_edge(qapp):
    tab, _sent = _make_tab()
    keys = set(tab._nodes)
    assert "dev:COM7" in keys
    assert any(k.startswith("tgt:") for k in keys)
    # An edge links the device to the target it discovered (device_source = COM7).
    assert tab._nodes["dev:COM7"]._edges, "device node should have an edge to its discovered target"


def test_device_node_command_routes_through_send_cmd(qapp):
    tab, sent = _make_tab()
    dev_node = tab._nodes["dev:COM7"]
    assert dev_node.actions, "device node should list firmware commands"
    # Firing the first action sends it to the right port via the send_cmd callback.
    _label, cb = dev_node.actions[0]
    cb()
    assert sent and sent[0][0] == "COM7"


def test_target_node_always_has_actions(qapp):
    tab, _sent = _make_tab()
    tgt_key = next(k for k in tab._nodes if k.startswith("tgt:"))
    # Real resolver actions when applicable, otherwise the honest "(no actions…)" fallback — never empty.
    assert tab._nodes[tgt_key].actions


def test_rebuild_preserves_dragged_position(qapp):
    tab, _sent = _make_tab()
    tab._nodes["dev:COM7"].setPos(123.0, 456.0)
    tab.rebuild()  # re-read after a "new scan" must NOT scramble the arranged web
    moved = tab._nodes["dev:COM7"]
    assert (round(moved.x()), round(moved.y())) == (123, 456)


def test_auto_arrange_resets_layout(qapp):
    tab, _sent = _make_tab()
    tab._nodes["dev:COM7"].setPos(999.0, 999.0)
    tab._auto_arrange()  # no skip -> full reset to the default fan-out
    moved = tab._nodes["dev:COM7"]
    assert (round(moved.x()), round(moved.y())) != (999, 999)
