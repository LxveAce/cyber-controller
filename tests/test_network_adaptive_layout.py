"""Wave-3 Batch C: the Network graph rescales its node geometry with the window (screen 7/7).

The DECISION is the pure `network_layout` (unit-tested in test_layout_profile) — the only decider
with screen-specific fields (node box size + truncation lengths, `node_h` floored to hit target).
Here the widget APPLIES it: nodes redraw at the tier's geometry, long labels truncate to the tier's
char budget, the toolbar hint demotes on dense chrome, and auto-arrange honors stack/columns.
Offscreen Qt.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QGraphicsSimpleTextItem  # noqa: E402

from src.ui.qt.layout_profile import layout_profile, network_layout  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_tab(with_data: bool = True):
    from src.core.action_resolver import ActionResolver
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.models.device import Device
    from src.models.target import Target, TargetType
    from src.ui.qt.network_tab import NetworkTab

    dm = DeviceManager()
    pool = TargetPool(EventBus())
    if with_data:
        dm.add_device(Device(port="COM7", name="Marauder", firmware="marauder", connected=True))
        pool.add(Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP, ssid="HomeNet",
                        channel=6, rssi=-40, device_source="COM7"))
    return NetworkTab(dm, pool, ActionResolver(dm), lambda port, cmd: None)


def _title_text(node) -> str:
    for ch in node.childItems():
        if isinstance(ch, QGraphicsSimpleTextItem):
            return ch.text()
    return ""


@pytest.mark.parametrize("w,node_w,node_h,title_chars", [
    (480, 132, 44, 18),    # compact
    (800, 150, 46, 22),    # regular
    (1440, 176, 52, 26),   # expanded
])
def test_node_geometry_scales_with_size(qapp, w, node_w, node_h, title_chars):
    tab = _make_tab(with_data=False)
    nl = network_layout(layout_profile(w, 800, touch=False, dpi=96))
    assert (nl.node_w, nl.node_h, nl.title_chars) == (node_w, node_h, title_chars)   # the contract
    tab._apply_network_layout(nl)
    node = tab._nodes["_placeholder"]
    assert (node.rect().width(), node.rect().height()) == (node_w, node_h)


def test_long_label_truncates_to_the_tier_budget(qapp):
    from src.ui.qt.network_tab import _Node
    long = "X" * 40
    compact = _Node(long, "", "node", [], 132, 44, 18, 22)
    expanded = _Node(long, "", "node", [], 176, 52, 26, 30)
    assert len(_title_text(compact)) == 18
    assert len(_title_text(expanded)) == 26


def test_dense_chrome_demotes_the_hint(qapp):
    tab = _make_tab(with_data=False)
    tab._apply_network_layout(network_layout(layout_profile(480, 800, touch=False, dpi=96)))
    assert tab._hint_label.isHidden()
    tab._apply_network_layout(network_layout(layout_profile(1440, 900, touch=False, dpi=96)))
    assert not tab._hint_label.isHidden()


def test_auto_arrange_drops_targets_below_devices_on_compact(qapp):
    # network_layout.stack: on a compact canvas the target fan drops BELOW the device column, not to
    # its right. Drive _auto_arrange directly (a full reset) so positions aren't preserved.
    tab = _make_tab()
    dev, tgt = tab._nodes["dev:COM7"], tab._nodes["tgt:AA:BB:CC:DD:EE:FF"]
    tab._net_geom = network_layout(layout_profile(480, 800, touch=False, dpi=96))   # compact
    tab._auto_arrange()
    assert tgt.y() > dev.y()      # dropped below the device column
    tab._net_geom = network_layout(layout_profile(1440, 900, touch=False, dpi=96))  # expanded
    tab._auto_arrange()
    assert tgt.x() > dev.x()      # fanned out to the right


def test_relayout_matches_the_resolver_and_debounces(qapp):
    tab = _make_tab(with_data=False)
    for w in (400, 1600):
        tab.resize(w, 800)
        qapp.processEvents()
        tab._relayout_network()
        p = layout_profile(max(1, tab.width()), max(1, tab.height()), touch=False,
                           dpi=tab.logicalDpiX() or 96)
        assert tab._nodes["_placeholder"].rect().width() == network_layout(p).node_w
    first = tab._last_network_size
    tab._relayout_network()   # same size class -> no-op
    assert tab._last_network_size == first
