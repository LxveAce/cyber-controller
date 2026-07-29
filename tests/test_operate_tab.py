"""Operate console (B16) — a button-driven single-device console: status-poll header, SAFE/ARMED
lamp, two-factor arm toggle, and a per-firmware TX-gated command grid. It is a poll-driven,
read-only view of shared Device state and writes through the Devices tab's guarded path. Offscreen.

These cover the console's invariants: the shared arm-lamp render, the TX-lockout (offensive-TX
buttons enabled only when ARMED), the arm-token send path, and DMS/disconnect gating of the poll.
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


class _FakeConn:
    """Captures writes; that's all the console's send/poll path touches."""

    def __init__(self) -> None:
        self.writes: list = []

    def write(self, data: str) -> None:
        self.writes.append(data)


class _FakeDM:
    """Minimal DeviceManager: one device + optional connection (list/get_device/get_connection)."""

    def __init__(self, dev, conn=None) -> None:
        self._dev = dev
        self._conn = conn

    def list_devices(self):
        return [self._dev]

    def get_device(self, port: str):
        return self._dev if self._dev.port == port else None

    def get_connection(self, port: str):
        return self._conn if (self._conn is not None and self._dev.port == port) else None


def _tab(dev, conn=None, dms_seen=None):
    from src.ui.qt.operate_tab import OperateTab

    tab = OperateTab(_FakeDM(dev, conn), dms_seen=dms_seen)
    tab._active_port = dev.port
    tab._grid_fw = ""          # force a grid (re)build for the device firmware on the next refresh
    tab._refresh()
    return tab


# ── shared render helper (no widget needed) ──────────────────────────

def test_console_uses_the_shared_arm_lamp_render():
    # The console and the Devices tab must render an identical lamp — same shared table.
    from src.ui.qt.arm_lamp import arm_lamp_render
    from src.ui.qt.device_tab import DeviceTab

    for state in ("safe", "pending", "armed", "tx_disabled", "", "future_state"):
        assert arm_lamp_render(state) == DeviceTab._arm_lamp_render(state)


# ── TX lockout: offensive-TX buttons only when ARMED ─────────────────

def test_tx_buttons_disabled_until_armed(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "safe"
    tab = _tab(dev)
    assert tab._tx_buttons, "lxveos catalog must contribute at least one offensive-TX button"
    assert all(not b.isEnabled() for b in tab._tx_buttons)   # SAFE -> TX locked
    assert all(b.isEnabled() for b in tab._safe_buttons)     # passive verbs available if connected


def test_tx_buttons_enable_when_armed(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "safe"
    tab = _tab(dev)
    assert all(not b.isEnabled() for b in tab._tx_buttons)

    dev.arm_state = "armed"                       # ingestor applied an `arm state=armed` line
    tab.on_line_received("COM23", "LXVEOS/1 arm state=armed")
    assert all(b.isEnabled() for b in tab._tx_buttons)   # ARMED -> offensive TX permitted
    assert "ARMED" in tab._arm_label.text()


def test_tx_buttons_stay_locked_when_disconnected(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=False)
    dev.arm_state = "armed"  # even a stale ARMED must not enable TX on a disconnected device
    tab = _tab(dev)
    assert all(not b.isEnabled() for b in tab._tx_buttons)
    assert all(not b.isEnabled() for b in tab._safe_buttons)


# ── arm-token send path ──────────────────────────────────────────────

def test_confirm_token_sends_arm_with_token(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "pending"
    conn = _FakeConn()
    tab = _tab(dev, conn)
    tab._token_edit.setText("428913")
    tab._on_confirm_token()
    assert conn.writes == ["arm 428913"]


def test_arm_and_disarm_buttons_send_bare_verbs(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    conn = _FakeConn()
    tab = _tab(dev, conn)
    tab._send("arm")
    tab._send("disarm")
    assert conn.writes == ["arm", "disarm"]


def test_send_without_connection_is_a_no_op(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    tab = _tab(dev, conn=None)  # no connection registered
    tab._send("arm")            # must not raise, just logs
    assert "no connection" in tab._log.toPlainText()


# ── auto-poll gating ─────────────────────────────────────────────────

def test_poll_sends_status_for_a_connected_lxveos_port(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    conn = _FakeConn()
    tab = _tab(dev, conn)
    tab._poll_tick()
    assert "status" in conn.writes  # lxveos defines a poll-safe `status` verb


def test_poll_skips_dms_gated_port(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    conn = _FakeConn()
    tab = _tab(dev, conn, dms_seen={"COM23"})  # a Dead-Man's-Switch prompt was seen here
    tab._poll_tick()
    assert conn.writes == []  # never auto-write to a DMS-gated port (could trip a wipe)


def test_poll_skips_disconnected_port(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=False)
    conn = _FakeConn()
    tab = _tab(dev, conn)
    tab._poll_tick()
    assert conn.writes == []


def test_poll_skips_firmware_without_a_status_verb(qapp):
    # A firmware whose catalog has no `status` must never get a stray auto-`status` write.
    from src.models.device import Device

    dev = Device(port="COM23", firmware="marauder", connected=True)
    conn = _FakeConn()
    tab = _tab(dev, conn)
    tab._poll_tick()
    assert "status" not in conn.writes


# ── defense-in-depth: _send re-checks arm state at write time ────────

def test_send_blocks_offensive_verb_when_not_armed(qapp, monkeypatch):
    # The button-enable gate can lag an armed->safe transition by up to the 2s poll; _send is the
    # authoritative backstop — an offensive-TX verb (danger != "") is refused unless armed now.
    from src.models.device import Device
    from src.protocols.base import CommandInfo
    from src.ui.qt import operate_tab

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "safe"
    conn = _FakeConn()
    tab = _tab(dev, conn)
    ci = CommandInfo("evilportal", "Offensive", "rogue AP + captive portal", danger="lab-only")
    tab._send("evilportal karma", ci)
    assert conn.writes == []                                   # blocked — device is SAFE
    assert "needs the device ARMED" in tab._log.toPlainText()
    # once the device is actually armed, the same send goes through (no confirm dialog in the test)
    monkeypatch.setattr(operate_tab.safety, "should_confirm", lambda *a, **k: False)
    dev.arm_state = "armed"
    tab._send("evilportal karma", ci)
    assert conn.writes == ["evilportal karma"]


# ── multi-device: TX re-locks on a device switch + other-port gating ─

class _MultiDM:
    """A device registry with several devices + optional per-port connections."""

    def __init__(self, devices, conns=None) -> None:
        self._devices = {d.port: d for d in devices}
        self._conns = conns or {}

    def list_devices(self):
        return list(self._devices.values())

    def get_device(self, port: str):
        return self._devices.get(port)

    def get_connection(self, port: str):
        return self._conns.get(port)


def test_tx_buttons_relock_on_switch_from_armed_to_safe(qapp):
    from src.models.device import Device
    from src.ui.qt.operate_tab import OperateTab

    armed = Device(port="COM23", firmware="lxveos", connected=True)
    armed.arm_state = "armed"
    safe = Device(port="COM99", firmware="lxveos", connected=True)
    safe.arm_state = "safe"
    tab = OperateTab(_MultiDM([armed, safe]))
    tab._active_port = "COM23"
    tab._grid_fw = ""
    tab._refresh()
    assert tab._tx_buttons and all(b.isEnabled() for b in tab._tx_buttons)  # armed -> TX enabled
    # switch to the SAFE device: the SAME buttons must re-lock (same firmware -> no grid rebuild)
    tab._active_port = "COM99"
    tab._refresh()
    assert all(not b.isEnabled() for b in tab._tx_buttons)


def test_on_line_received_from_other_port_does_not_repaint(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "safe"
    tab = _tab(dev)
    assert all(not b.isEnabled() for b in tab._tx_buttons)  # SAFE -> locked
    # a line from a DIFFERENT port must NOT repaint this console (else it would read the now-armed
    # active device and wrongly enable TX)
    dev.arm_state = "armed"
    tab.on_line_received("COM99", "LXVEOS/1 arm state=armed")
    assert all(not b.isEnabled() for b in tab._tx_buttons)  # unchanged — repaint gated on the port


# ── firmware without an arm concept: total functionality behind a confirm ─────
# Owner directive 2026-07-21 (authorized lab use): offensive commands must fire on EVERY firmware.
# Firmwares that don't implement arming (Marauder/DIV/GhostESP/Bruce) are confirm-gated, not dead-ended.

def test_non_arming_firmware_tx_buttons_enabled_when_connected(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="marauder", connected=True)
    tab = _tab(dev)
    assert tab._tx_buttons, "marauder has offensive verbs -> classify() must flag them as TX buttons"
    # No arm concept -> the offensive buttons are usable the moment the device is connected.
    assert all(b.isEnabled() for b in tab._tx_buttons)
    assert all(b.isEnabled() for b in tab._safe_buttons)
    # The two-factor arm box is hidden (it would be three dead buttons on a non-arming firmware).
    # isHidden() reflects the explicit hide regardless of the offscreen parent's visibility.
    assert tab._arm_box.isHidden()


def test_non_arming_firmware_dangerous_send_confirms_then_sends(qapp, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox

    from src.models.device import Device

    dev = Device(port="COM23", firmware="marauder", connected=True)
    conn = _FakeConn()
    tab = _tab(dev, conn)
    # The confirm dialog must appear (not a hard block); accept it.
    seen = {"asked": False}

    def _fake_warning(*_a, **_k):
        seen["asked"] = True
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(_fake_warning))
    tab._send("attack -t deauth")
    assert seen["asked"], "a dangerous command on non-arming firmware must confirm, not hard-block"
    assert conn.writes == ["attack -t deauth"]


def test_arming_firmware_still_shows_arm_box_and_gates_tx(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.arm_state = "safe"
    tab = _tab(dev)
    # LxveOS arms -> the arm box is shown (not hidden) and offensive TX stays locked until ARMED.
    assert not tab._arm_box.isHidden()
    assert all(not b.isEnabled() for b in tab._tx_buttons)


# ── LxveNode Link strip: read-only tier/quality telemetry, hidden without a relay link ────────

def test_link_strip_hidden_when_device_has_no_link(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    tab = _tab(dev)
    # A plain USB target reports no link -> the strip is explicitly hidden (not just off-screen).
    assert tab._link_label.isHidden()


def test_link_strip_shows_tier_when_a_relay_link_is_reported(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    tab = _tab(dev)
    dev.apply_link_state({"link_event": "link", "tier": "lora", "rssi": -104,
                          "dr": "sf9bw125", "up": True})
    tab._refresh()
    assert not tab._link_label.isHidden()
    assert "LoRa" in tab._link_label.text()
    assert "sf9bw125" in tab._link_label.text()


# ── Tier-aware poll cadence: the timer lengthens on a constrained LoRa link ────────────────────

def test_poll_interval_is_base_without_a_link(qapp):
    from src.models.device import Device
    from src.ui.qt.link_strip import POLL_BASE_MS

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    tab = _tab(dev)
    assert tab._timer.interval() == POLL_BASE_MS


def test_poll_interval_throttles_on_a_lora_link_and_recovers(qapp):
    from src.models.device import Device
    from src.ui.qt.link_strip import POLL_BASE_MS, POLL_THROTTLED_MS

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    tab = _tab(dev)
    dev.apply_link_state({"link_event": "link", "tier": "lora", "up": True})
    tab._refresh()
    assert tab._timer.interval() == POLL_THROTTLED_MS   # constrained mesh link -> back off the poll
    # Failover back to Wi-Fi restores the fast cadence (tracked off the `tier` frame's to= field).
    dev.apply_link_state({"link_event": "tier", "from": "lora", "to": "wifi", "reason": "rssi"})
    tab._refresh()
    assert tab._timer.interval() == POLL_BASE_MS


# ── Tier-aware stream gate: high-bandwidth verbs off on LoRa, live on Wi-Fi ────────────────────

def test_stream_buttons_disabled_on_a_lora_link(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    tab = _tab(dev)
    assert tab._stream_buttons, "lxveos marks sniff/capture/wardrive as stream verbs"
    # On Wi-Fi (or no link) a connected device's recon-stream verbs are live...
    dev.apply_link_state({"link_event": "link", "tier": "wifi", "up": True})
    tab._refresh()
    assert all(b.isEnabled() for b in tab._stream_buttons)
    # ...but a constrained LoRa link disables them (the link can't carry a live capture/monitor).
    dev.apply_link_state({"link_event": "tier", "from": "wifi", "to": "lora", "reason": "rssi"})
    tab._refresh()
    assert all(not b.isEnabled() for b in tab._stream_buttons)
    assert "LoRa link" in tab._stream_buttons[0].toolTip()


def test_stream_gate_does_not_touch_non_stream_verbs(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    tab = _tab(dev)
    dev.apply_link_state({"link_event": "link", "tier": "lora", "up": True})
    tab._refresh()
    # A passive non-stream recon verb (e.g. `scan`) stays enabled on LoRa — only the firehose is gated.
    non_stream_safe = [b for b in tab._safe_buttons if b not in tab._stream_buttons]
    assert non_stream_safe and all(b.isEnabled() for b in non_stream_safe)
