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


# ── DV1: aspect-lock the pop-out (kill the resize bandaid) ────────────
def test_device_view_aspect_contract(qapp):
    v = DeviceView(DeviceScreenModel("ESP32 Marauder", marauder_menu()))
    assert v.hasHeightForWidth() is True
    assert v.sizePolicy().hasHeightForWidth() is True       # layouts will honor the ratio too
    # 240x320 native => height follows width at exactly 4:3
    assert v.heightForWidth(240) == 320
    assert v.heightForWidth(480) == 640
    assert v.heightForWidth(300) == round(300 * 320 / 240)
    assert v.minimumSizeHint() == v.NATIVE
    sh = v.sizeHint()
    assert sh.height() == v.heightForWidth(sh.width())      # the hint is itself on-ratio


def test_device_view_window_snaps_to_aspect(qapp):
    """A standalone Device-View window must not letterbox: a wrong-aspect resize snaps back to the native
    ratio (the fix for the owner's 'resize bandaid'). heightForWidth alone can't do this for a top-level
    widget, so _lock_aspect() (called from resizeEvent) enforces it."""
    v = DeviceView(DeviceScreenModel("ESP32 Marauder", marauder_menu()))
    v.show()
    qapp.processEvents()
    try:
        assert v.isWindow()
        v.resize(600, 400)                                  # deliberately letterboxed (requested 3:2)
        qapp.processEvents()
        # the resizeEvent path snapped it back to the native ratio on its own — height followed width
        assert v.width() == 600
        assert abs(v.height() - v.heightForWidth(600)) <= 1   # 800, not the requested 400 -> no dead-space
        h = v.height()
        v._lock_aspect()                                    # idempotent when already on-ratio
        assert v.height() == h
    finally:
        v.close()


def test_device_view_window_snaps_non_multiple_width(qapp):
    v = DeviceView(DeviceScreenModel("ESP32 Marauder", marauder_menu()))
    v.show()
    qapp.processEvents()
    try:
        v.resize(601, 400)                                  # 601 isn't a clean multiple of 3
        qapp.processEvents()
        assert v.width() == 601
        assert abs(v.height() - v.heightForWidth(601)) <= 1   # 801, within the 1px tolerance
    finally:
        v.close()


def test_device_view_window_snap_respects_minimum(qapp):
    """Snapping never drives the window below its native minimum: a sub-minimum width clamps to 240×320
    (itself exactly on-ratio) rather than colliding with the height floor."""
    v = DeviceView(DeviceScreenModel("ESP32 Marauder", marauder_menu()))
    v.show()
    qapp.processEvents()
    try:
        v.resize(100, 900)                                  # width below the 240 minimum
        qapp.processEvents()
        assert v.width() == v.NATIVE.width()                # clamped to 240
        assert abs(v.height() - v.heightForWidth(v.width())) <= 1   # and on-ratio (320)
    finally:
        v.close()


def test_device_view_embedded_does_not_self_resize(qapp):
    """When embedded in a layout (not a top-level window), _lock_aspect is a no-op (the isWindow() guard) so
    it never fights the parent — the self-snap is a standalone-window fix only."""
    from PyQt5.QtWidgets import QVBoxLayout, QWidget

    host = QWidget()
    QVBoxLayout(host).addWidget(DeviceView(DeviceScreenModel("ESP32 Marauder", marauder_menu())))
    v = host.findChild(DeviceView)
    host.resize(500, 500)
    host.show()
    qapp.processEvents()
    try:
        assert v.isWindow() is False
        before = (v.width(), v.height())
        v._lock_aspect()                                    # must NOT self-resize an embedded instance
        assert (v.width(), v.height()) == before
    finally:
        host.close()


def test_device_view_maximized_is_not_aspect_locked(qapp):
    """A maximized/fullscreen window keeps the WM geometry — the portrait ratio must not force it taller
    than the screen. (Skips where the offscreen platform can't report a maximized state.)"""
    v = DeviceView(DeviceScreenModel("ESP32 Marauder", marauder_menu()))
    v.showMaximized()
    qapp.processEvents()
    if not (v.isMaximized() or v.isFullScreen()):
        pytest.skip("offscreen platform does not report a maximized state")
    try:
        w, h = v.width(), v.height()
        v._lock_aspect()
        assert (v.width(), v.height()) == (w, h)            # guard held — geometry untouched
    finally:
        v.close()
