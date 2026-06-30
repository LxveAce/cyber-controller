"""Device View — reconstructed firmware-UI skin (src/ui/qt/device_view.py).

Covers the model navigation, the offscreen-testable pure render, and — importantly — that every menu leaf
in the Marauder skin maps to a REAL Marauder serial command (so the reconstruction drives the actual
firmware, not invented commands). Runs offscreen.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402
from PyQt5.QtGui import QImage  # noqa: E402

from src.ui.qt.device_view import (  # noqa: E402
    SKINS,
    DeviceScreenModel,
    DeviceView,
    MenuNode,
    esp32div_menu,
    ghostesp_menu,
    marauder_menu,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _leaf_commands(nodes):
    out = []
    for n in nodes:
        if n.is_menu:
            out += _leaf_commands(n.children)
        elif n.command:
            out.append(n.command)
    return out


def test_every_marauder_leaf_is_a_real_command():
    from src.protocols.marauder import MarauderProtocol
    real = {c.name for c in MarauderProtocol().get_commands()}
    # CommandInfo.name holds the command string (e.g. "scanap", "attack -t deauth")
    for cmd in _leaf_commands(marauder_menu()):
        assert cmd in real, f"skin leaf {cmd!r} is not a real Marauder command"


def test_every_ghostesp_leaf_is_a_real_command():
    from src.protocols.ghost_esp import GhostESPProtocol
    real = {c.name for c in GhostESPProtocol().get_commands()}
    for cmd in _leaf_commands(ghostesp_menu()):
        assert cmd in real, f"skin leaf {cmd!r} is not a real GhostESP command"


def test_every_esp32div_leaf_is_a_real_command():
    from src.protocols.esp32_div import Esp32DivProtocol
    real = {c.name for c in Esp32DivProtocol().get_commands()}
    for cmd in _leaf_commands(esp32div_menu()):
        assert cmd in real, f"skin leaf {cmd!r} is not a real ESP32-DIV command"


def test_skins_registry_builds_and_renders(qapp):
    for key, (title, factory) in SKINS.items():
        m = DeviceScreenModel(title, factory())
        img = DeviceView(m).render_native()
        assert (img.width(), img.height()) == (240, 320), key


def test_model_navigation():
    m = DeviceScreenModel("ESP32 Marauder", marauder_menu())
    assert m.crumb() == "Main Menu"
    assert [n.label for n in m.items()] == ["WiFi", "Bluetooth", "Device"]
    m.down()
    assert m.sel == 1
    m.up(); m.up()           # wraps
    assert m.sel == 2
    m.sel = 0
    m.enter()                # into WiFi (a submenu)
    assert m.crumb() == "WiFi"
    assert "Scan APs" in [n.label for n in m.items()]
    m.back()
    assert m.crumb() == "Main Menu" and m.sel == 0


def test_enter_leaf_sends_command():
    sent = []
    def send(c):
        sent.append(c)
        return True          # actually delivered
    m = DeviceScreenModel("ESP32 Marauder", marauder_menu())
    m.enter()                # WiFi
    m.sel = 0
    m.enter(send)            # Scan APs -> command (v1.12.3: scanall)
    assert sent == ["scanall"]
    assert m.status.endswith("scanall") and "sent" in m.status


def test_enter_leaf_preview_when_not_delivered():
    m = DeviceScreenModel("ESP32 Marauder", marauder_menu())
    m.enter()
    m.sel = 0
    m.enter(lambda c: False)  # no device -> preview, not "sent"
    assert m.status.startswith("preview")


def test_render_native_is_a_real_image(qapp):
    m = DeviceScreenModel("ESP32 Marauder", marauder_menu())
    v = DeviceView(m)
    img = v.render_native()
    assert isinstance(img, QImage)
    assert (img.width(), img.height()) == (240, 320)
    # the selected row is painted neon-green -> at least some non-background pixels exist
    bg = img.pixel(2, 120)  # somewhere likely background
    distinct = sum(1 for y in range(44, 70) for x in range(0, 240, 20) if img.pixel(x, y) != bg)
    assert distinct > 0


def test_widget_constructs_and_grabs_offscreen(qapp):
    m = DeviceScreenModel("ESP32 Marauder", marauder_menu())
    v = DeviceView(m)
    v.resize(480, 640)
    pix = v.grab()           # must not raise; paintEvent path exercised
    assert not pix.isNull()


def test_custom_menu_node():
    n = MenuNode("X", children=[MenuNode("Y", command="info")])
    assert n.is_menu and not n.children[0].is_menu
