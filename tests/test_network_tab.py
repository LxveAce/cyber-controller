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


def test_reflash_refreshes_graph_via_device_poll(qapp):
    """Regression (#5, "network graph went stale after reflashing"): the graph only reacted to
    target.* bus events, and DeviceManager emits NO device-lifecycle events — so a reflashed board
    (disconnect→reconnect, re-detected with new firmware) fired nothing the graph heard and it
    froze. The device poll must rebuild when the fingerprint changes, and NOT churn otherwise."""
    tab, _sent = _make_tab()   # device COM7 @ firmware "marauder", connected
    tab.isVisible = lambda: True   # deterministic: exercise the poll without offscreen show()

    calls = []
    real_rebuild = tab.rebuild

    def _spy():
        calls.append(1)
        real_rebuild()

    tab.rebuild = _spy

    # Unchanged device set -> poll is a no-op (cheap; no rebuild churn every tick).
    tab._poll_devices()
    assert calls == [], "poll must not rebuild when the device set is unchanged"

    # Simulate a reflash: the board comes back re-detected with different firmware.
    dev = tab._dm.get_device("COM7")
    dev.firmware = "ghostesp"
    assert tab._device_fingerprint() != tab._device_fp, "a firmware change must move the fp"
    tab._poll_devices()
    assert calls == [1], "poll must rebuild exactly once when the device changed (graph un-stales)"
    assert tab._device_fp == tab._device_fingerprint(), "rebuild must refresh the recorded fp"

    # Now that the graph reflects the current device set, further ticks must not keep rebuilding.
    tab._poll_devices()
    assert calls == [1], "poll must not re-fire once the fingerprint is current again"


def test_device_disconnect_reconnect_cycle_refreshes_graph(qapp):
    """A reflash tears the link down then brings it back — connected True→False→True must each
    move the fingerprint so the graph tracks the board across the cycle (not just a fw change)."""
    tab, _sent = _make_tab()
    tab.isVisible = lambda: True
    dev = tab._dm.get_device("COM7")

    fp_connected = tab._device_fp
    dev.connected = False                     # disconnect (start of reflash)
    assert tab._device_fingerprint() != fp_connected
    tab._poll_devices()
    fp_disconnected = tab._device_fp
    assert fp_disconnected == tab._device_fingerprint()

    dev.connected = True                      # reconnect (flash done, board back)
    assert tab._device_fingerprint() != fp_disconnected
    calls = []
    real = tab.rebuild
    tab.rebuild = lambda: (calls.append(1), real())[1]
    tab._poll_devices()
    assert calls == [1], "the reconnect after a reflash must rebuild the graph"


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


def test_device_node_gates_danger_and_skips_templates(qapp, monkeypatch):
    """The experimental Network tab is a REAL send surface, so it must not (1) bind unfilled command
    templates as raw sends (that would transmit the literal '<idx>' to the radio), nor (2) fire a
    dangerous command with no safety confirmation — every other send path gates deauth/jam/spam."""
    monkeypatch.setattr("src.config.settings.load_settings", lambda: {})  # defaults -> confirm dangerous
    tab, sent = _make_tab()
    dev_node = tab._nodes["dev:COM7"]
    labels = [lbl for lbl, _cb in dev_node.actions]

    # (1) no unfilled-template command is offered as a raw send
    assert labels, "device node should still list its non-template commands"
    assert not any("<" in lbl or ">" in lbl for lbl in labels), labels

    # (2) a dangerous command is gated: user answers No -> nothing is sent
    from PyQt5.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.No))
    deauth = next(cb for lbl, cb in dev_node.actions if lbl == "attack -t deauth")
    deauth()
    assert ("COM7", "attack -t deauth") not in sent

    # (3) a safe command still routes unchanged
    scan = next(cb for lbl, cb in dev_node.actions if lbl == "scanall")
    scan()
    assert ("COM7", "scanall") in sent


def test_target_action_gates_danger(qapp, monkeypatch):
    """A target action (Deauth AP / Beacon Clone / Karma evil-twin) fired from the Network tab is a real
    attack send and must clear the SAME danger confirmation as a device command — otherwise the tab is a
    silent bypass that dispatches attack commands with no confirmation."""
    monkeypatch.setattr("src.config.settings.load_settings", lambda: {})  # defaults -> confirm dangerous

    # execute_action is imported lazily inside _run_target_action, so patch it on the source module and
    # record every dispatch instead of touching real hardware.
    calls: "list[tuple[str, str]]" = []
    monkeypatch.setattr(
        "src.core.action_resolver.execute_action",
        lambda action, port, dm, **k: calls.append((getattr(action, "command_template", ""), port)),
    )

    tab, _sent = _make_tab()
    from PyQt5.QtWidgets import QMessageBox  # noqa: E402
    from src.models.action import ActionCategory, TargetAction  # noqa: E402

    attack = TargetAction(
        "Deauth AP", "attack -t deauth", "x", ActionCategory.ATTACK, pre_commands=["select -a 0"],
    )

    # Dangerous target action, user answers No -> it must NOT be dispatched.
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.No))
    tab._run_target_action(attack, "COM7")
    assert calls == [], "a dangerous target action must be gated; answering No aborts the send"

    # Same action, user answers Yes -> it dispatches through execute_action.
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Yes))
    tab._run_target_action(attack, "COM7")
    assert calls == [("attack -t deauth", "COM7")]

    # A non-attack (safe) action is never gated: it dispatches even though 'warning' would answer No.
    safe = TargetAction("Monitor Channel", "sniffraw", "x", ActionCategory.MONITOR)
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.No))
    tab._run_target_action(safe, "COM7")
    assert ("sniffraw", "COM7") in calls


# ── WS-5: interaction fixes (clamped zoom + drag-to-pan the canvas) ──

def test_graph_zoom_ok_bounds():
    # Pure clamp logic: block a notch only when already at/past a bound and pushing further past it.
    from src.ui.qt.network_tab import _GRAPH_MAX_SCALE, _GRAPH_MIN_SCALE, _graph_zoom_ok
    assert _graph_zoom_ok(1.0, True) and _graph_zoom_ok(1.0, False)     # mid-band: both directions OK
    assert not _graph_zoom_ok(_GRAPH_MAX_SCALE, True)                   # at ceiling: no more zoom-in
    assert _graph_zoom_ok(_GRAPH_MAX_SCALE, False)                      # ...but can zoom back out
    assert not _graph_zoom_ok(_GRAPH_MIN_SCALE, False)                  # at floor: no more zoom-out
    assert _graph_zoom_ok(_GRAPH_MIN_SCALE, True)                       # ...but can zoom back in
    assert _graph_zoom_ok(0.0001, True)                                 # a below-floor fit-scale zooms in


def test_graph_wheel_zoom_is_clamped(qapp):
    # Hammering the wheel must not fling the scale into the void — it stays within ~one notch of the bounds.
    from PyQt5.QtCore import QPoint
    from PyQt5.QtWidgets import QGraphicsScene

    from src.ui.qt.network_tab import _GRAPH_MAX_SCALE, _GRAPH_MIN_SCALE, _GraphView

    class _Wheel:
        def __init__(self, dy):
            self._dy = dy

        def angleDelta(self):
            return QPoint(0, self._dy)

        def accept(self):
            pass

    view = _GraphView(QGraphicsScene())
    for _ in range(200):
        view.wheelEvent(_Wheel(120))                       # zoom in hard
    assert view.transform().m11() <= _GRAPH_MAX_SCALE * 1.16
    for _ in range(400):
        view.wheelEvent(_Wheel(-120))                      # then zoom out hard
    assert view.transform().m11() >= _GRAPH_MIN_SCALE / 1.16


def test_graph_pans_empty_space_but_not_nodes(qapp, monkeypatch):
    # Left-drag on empty background pans the canvas; a press over a node does NOT pan (node keeps its drag).
    from PyQt5.QtCore import QEvent, QPointF, Qt
    from PyQt5.QtGui import QMouseEvent
    from PyQt5.QtWidgets import QGraphicsScene

    from src.ui.qt.network_tab import _GraphView

    view = _GraphView(QGraphicsScene())

    def _ev(kind):
        return QMouseEvent(kind, QPointF(5, 5), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)

    monkeypatch.setattr(view, "itemAt", lambda _p: None)   # empty background
    view.mousePressEvent(_ev(QEvent.MouseButtonPress))
    assert view._panning
    view.mouseReleaseEvent(_ev(QEvent.MouseButtonRelease))
    assert not view._panning

    monkeypatch.setattr(view, "itemAt", lambda _p: object())  # over a node
    view.mousePressEvent(_ev(QEvent.MouseButtonPress))
    assert not view._panning, "a press on a node must not start a canvas pan"
