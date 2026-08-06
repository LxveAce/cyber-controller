"""OperateConsole (src/ui/qt/operate_console.py) — the reform OPERATE Console (OperateTab relayout).

Structural + behavioural checks that the band re-layout keeps the inherited guarded machinery intact
and honours the pinned REFORM-CONSOLE-SPEC: the init-order (pre-super injection) holds, the same
widget attrs exist so _refresh/_rebuild_grid/_apply_operate_layout still drive them, the three bands
are laid out (only inner panes scroll), the pills honest-hide BlueJammer, Zone B rebuilds ONLY on a
(port,fw) change (never on the poll) while readiness refreshes every poll, only one OpPanel can
co-render, Zone B routes through the console's own guarded callables, the BlueJammer arm gate stays
independent of arm_state, and compact densification hides telemetry/link while the lamp stays.
Offscreen Qt; no TX.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QHBoxLayout, QTextEdit  # noqa: E402

from src.ui.qt.operate_console import OperateConsole  # noqa: E402
from src.ui.qt.pill_pane_stack import PillPaneStack  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _console():
    from src.core.broadcast import BroadcastEngine
    from src.core.cross_comm import EventBus
    from src.core.device_manager import DeviceManager
    dm = DeviceManager()
    bus = EventBus()
    return OperateConsole(dm, BroadcastEngine(dm, bus), bus)


class _Dev:
    def __init__(self, port="COM_X", firmware="", connected=True):
        self.port, self.firmware, self.connected = port, firmware, connected


def test_init_order_no_attributeerror(qapp):
    """Pre-super attribute injection: OperateTab.__init__ runs our _build_ui + _refresh during
    construction — every attr they read must exist. Construction must not raise."""
    c = _console()
    assert isinstance(c, OperateConsole)


def test_inherited_widget_attrs_present(qapp):
    c = _console()
    for attr in ("_device_combo", "_fw_combo", "_link_label", "_telemetry_label", "_arm_label",
                 "_arm_box", "_btn_arm", "_btn_confirm", "_btn_disarm", "_grid_box", "_grid_layout",
                 "_op_detail_box", "_op_detail_layout", "_op_hint", "_log"):
        assert getattr(c, attr, None) is not None, f"missing inherited widget {attr}"
    assert isinstance(c._head_row, QHBoxLayout)   # base _apply_operate_layout flips its direction


def test_three_bands_no_outer_scroll(qapp):
    """Top band + pills + log are the outer layout's three items, in order; the console is NOT
    wrapped in one big QScrollArea (only inner panes scroll — no-scroll-to-find-a-feature)."""
    c = _console()
    lay = c.layout()
    assert lay.count() == 3
    assert lay.itemAt(1).widget() is c._pills and isinstance(c._pills, PillPaneStack)
    assert isinstance(lay.itemAt(2).widget(), QTextEdit)   # bottom band = the activity log
    from PyQt5.QtWidgets import QScrollArea
    assert not isinstance(lay.itemAt(0).widget(), QScrollArea)


def test_pills_default_two(qapp):
    c = _console()
    assert c._pills.keys() == ["single", "broadcast"]


def test_bluejammer_pill_honest_hidden(qapp, monkeypatch):
    """The BlueJammer pill appears only when the active device's RESOLVED protocol is bluejammer,
    and disappears when it isn't — keyed off protocol_name (spec §1 / critic)."""
    c = _console()
    monkeypatch.setattr(c, "_active_device", lambda: _Dev(firmware="bluejammer"))
    c._sync_pills()
    assert "bluejammer" in c._pills.keys()
    monkeypatch.setattr(c, "_active_device", lambda: _Dev(firmware="marauder"))
    c._sync_pills()
    assert "bluejammer" not in c._pills.keys() and c._pills.keys() == ["single", "broadcast"]


def test_zone_b_set_actions_only_on_key_change(qapp, monkeypatch):
    """Zone B rebuilds (set_actions) ONLY on a (port,fw) change — never on the poll (a rebuild would
    tear down an open OpPanel). refresh_readiness runs every poll instead."""
    c = _console()
    calls = {"set_actions": 0, "refresh": 0}
    monkeypatch.setattr(c._zone_b_strip, "set_actions",
                        lambda *a, **k: calls.__setitem__("set_actions", calls["set_actions"] + 1))
    monkeypatch.setattr(c._zone_b_strip, "refresh_readiness",
                        lambda: calls.__setitem__("refresh", calls["refresh"] + 1))
    monkeypatch.setattr(c, "_active_device", lambda: _Dev(port="COM7", firmware="marauder"))
    # Stub the nav-only prime: with an empty test DeviceManager the real select_device ->
    # _reload_devices would reset _active_port and oscillate the (port,fw) key (a test artifact).
    monkeypatch.setattr(c, "select_device", lambda port: True)
    c._active_port = "COM7"
    c._sync_zone_b()
    c._sync_zone_b()
    c._sync_zone_b()
    assert calls["set_actions"] == 1, "set_actions must fire once for a stable (port,fw)"
    assert calls["refresh"] == 3, "refresh_readiness must fire every poll"
    monkeypatch.setattr(c, "_active_device", lambda: _Dev(port="COM7", firmware="flipper"))
    c._sync_zone_b()
    assert calls["set_actions"] == 2, "a firmware change must rebuild Zone B"


def test_single_op_panel_no_co_render(qapp):
    """Two OpPanels can never co-render: the Console strip routes an arg tile to the SHARED
    right-pane OpPanel (console._on_command_selected), never its own inline _open_panel."""
    c = _console()
    routed = []
    c._zone_b_strip._arg_target = routed.append   # spy the shared-panel route

    class _Ci:
        name, args, description = "scan_ap", "channel", "scan"
    ci = _Ci()
    c._zone_b_strip._on_tile(ci)
    assert routed == [ci]                          # arg tile -> shared right-pane OpPanel
    assert c._zone_b_strip._open_panel is None     # the strip never opened its own inline OpPanel


def test_zone_b_wired_to_guarded_console_callables(qapp, monkeypatch):
    """Zone B is wired with the console's OWN guarded callables (run_curated / _send / ready_for /
    safe_state) — so migrated verbs inherit ARM + the write-time tx_hard_block (critic #8), never a
    re-implemented send path."""
    c = _console()
    captured = {}
    monkeypatch.setattr(
        c._zone_b_strip, "set_actions",
        lambda cis, run_fn, send, ready_fn, safe_fn, **k:
        captured.update(run_fn=run_fn, send=send, ready_fn=ready_fn, safe_fn=safe_fn))
    monkeypatch.setattr(c, "_active_device", lambda: _Dev(port="COM7", firmware="marauder"))
    c._active_port = "COM7"
    c._rebuild_zone_b("COM7", "marauder", True)
    assert captured["run_fn"] == c.run_curated
    assert captured["send"] == c._send
    assert captured["ready_fn"] == c.ready_for
    assert captured["safe_fn"] == c.safe_state


def test_bluejammer_gate_independent_of_arm_state(qapp):
    """critic HIGH: the BlueJammer panel's Arm buttons enable on attestation alone — never coupled
    to the console arm_state (which stays SAFE for a bluejammer, as it has no supports_arm)."""
    c = _console()
    bj = c._bj_panel
    assert all(not b.isEnabled() for b in bj._bj_arm_btns)   # SAFE by default, attest unchecked
    bj._bj_attest.setChecked(True)                            # console arm_state untouched
    assert all(b.isEnabled() for b in bj._bj_arm_btns)


def test_compact_densification(qapp):
    """critic LOW: on a compact deck the telemetry + link collapse but the SAFE/ARMED lamp stays."""
    from src.ui.qt.layout_profile import layout_profile, operate_layout
    c = _console()
    c._apply_operate_layout(operate_layout(layout_profile(480, 800, dpi=96)))   # compact class
    assert c._compact is True
    assert not c._telemetry_label.isVisible()
    assert not c._link_label.isVisible()
    assert c._arm_label.isVisibleTo(c)          # the lamp is never collapsed


def test_select_device_prime_is_nav_only(qapp, monkeypatch):
    """_rebuild_zone_b primes with select_device (nav-only) — never sends or sets arm_state."""
    c = _console()
    sent = []
    monkeypatch.setattr(c, "_send", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(c._zone_b_strip, "set_actions", lambda *a, **k: None)
    monkeypatch.setattr(c, "_active_device", lambda: _Dev(port="COM7", firmware="marauder"))
    c._active_port = "COM7"
    c._rebuild_zone_b("COM7", "marauder", True)
    assert sent == [], "priming Zone B must not send anything"
