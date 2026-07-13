"""Devices-tab capability chips — the caps line prefers a connected device's RUNTIME capabilities
(a LxveOS status/info line updates them live) over the selected firmware's static map, refreshes on
each incoming line, and renders unknown future-bit tokens (``capN``) muted + distinct. Offscreen Qt.

Builds on the beat-226 data path (Device.apply_device_info / Device.capabilities / ingestor route);
this is the Devices-tab surface that makes those live caps visible where a board is connected.
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


# ── pure render helper (no widget needed) ────────────────────────────

def test_caps_chip_html_upper_cases_named_slugs():
    from src.ui.qt.device_tab import DeviceTab

    html = DeviceTab._caps_chip_html(["ble", "bt_classic", "wifi"])
    assert "WIFI" in html and "BLE" in html and "BT_CLASSIC" in html
    assert html.startswith("Capabilities:")
    assert "unknown cap" not in html  # nothing unknown here


def test_caps_chip_html_renders_unknown_bit_muted_and_distinct():
    from src.ui.qt.device_tab import DeviceTab

    html = DeviceTab._caps_chip_html(["cap10", "wifi"])
    # The named slug is a plain upper token; the unknown future bit is labelled + styled distinctly.
    assert "WIFI" in html
    assert "unknown cap 10" in html
    assert "font-style:italic" in html and "#6e7681" in html
    # 'cap10' must NOT leak through as a peer named chip.
    assert "CAP10" not in html


def test_caps_chip_html_empty_is_blank():
    from src.ui.qt.device_tab import DeviceTab

    assert DeviceTab._caps_chip_html([]) == ""


# ── live surface on the tab ──────────────────────────────────────────

def _tab_with_device(dev):
    from src.core.device_manager import DeviceManager
    from src.ui.qt.device_tab import DeviceTab

    dm = DeviceManager()
    dm.add_device(dev)
    tab = DeviceTab(dm)
    tab._active_port = dev.port
    return tab


def test_caps_line_prefers_connected_device_runtime_caps(qapp):
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.runtime_capabilities = frozenset({"wifi", "ble", "bt_classic"})
    tab = _tab_with_device(dev)
    tab._update_capabilities()
    txt = tab._caps_label.text()
    assert "WIFI" in txt and "BLE" in txt and "BT_CLASSIC" in txt


def test_caps_line_falls_back_to_static_map_without_runtime(qapp):
    from src.models.device import Device
    from src.protocols import capabilities_for

    dev = Device(port="COM7", firmware="marauder", connected=True)  # no runtime caps reported
    tab = _tab_with_device(dev)
    # Force the combo to the device's firmware so the static map is marauder's.
    tab._update_capabilities()
    txt = tab._caps_label.text()
    static = capabilities_for("marauder")
    assert static  # marauder declares static capabilities
    # At least one static token surfaces (upper-cased) — proving the fallback path.
    assert any(tok.upper() in txt for tok in static)


def test_incoming_line_refreshes_caps_from_device(qapp):
    # The live-refresh wire: the ingestor updates the Device on the serial thread; _on_line_received
    # then runs on the Qt thread and must repaint the caps line from the (now-updated) Device — no
    # new signal. Simulate by mutating the Device (as the ingestor would) then delivering a line.
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    tab = _tab_with_device(dev)
    tab._update_capabilities()
    assert "WIFI" not in tab._caps_label.text()  # nothing reported yet

    dev.runtime_capabilities = frozenset({"wifi", "ble", "bt_classic"})  # ingestor applied status
    tab._on_line_received("COM23", "LXVEOS/1 status ... caps=0x007 ...")
    assert "WIFI" in tab._caps_label.text() and "BT_CLASSIC" in tab._caps_label.text()


def test_incoming_line_from_other_port_does_not_repaint_active(qapp):
    # A line from a non-active port must not repaint the active node's caps line.
    from src.models.device import Device

    dev = Device(port="COM23", firmware="lxveos", connected=True)
    dev.runtime_capabilities = frozenset({"wifi"})
    tab = _tab_with_device(dev)
    tab._update_capabilities()
    before = tab._caps_label.text()
    # A different port speaks; even though we mutate COM23, the guard is the port check.
    dev.runtime_capabilities = frozenset({"wifi", "ble"})
    tab._on_line_received("COM99", "noise from another device")
    assert tab._caps_label.text() == before  # unchanged — the refresh is gated on the active port
