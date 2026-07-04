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
    SkinSpec,
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


# ── DV3: per-firmware SkinSpec (palette JSON) ────────────────────────
def test_dv3_skins_have_distinct_specs(qapp):
    specs = {sid: SkinSpec.load(sid) for sid in SKINS}
    assert len({s.bg.name() for s in specs.values()}) == 3       # each firmware's bg differs
    assert len({s.accent.name() for s in specs.values()}) == 3   # each firmware's accent differs
    default = SkinSpec()
    assert all(s.accent.name() != default.accent.name() for s in specs.values())  # actually customized


def test_dv3_render_differs_per_skin(qapp):
    def render(sid):
        title, factory = SKINS[sid]
        return DeviceView(DeviceScreenModel(title, factory(), skin=sid)).render_native()

    a, b, c = render("marauder"), render("ghostesp"), render("esp32div")
    assert a != b and b != c and a != c          # QImage != is a pixel-wise compare -> palettes truly differ


def test_dv3_default_skin_unchanged_violet(qapp):
    # No skin id -> the built-in default palette, so pre-DV3 renders are byte-identical.
    v = DeviceView(DeviceScreenModel("ESP32 Marauder", marauder_menu()))
    assert v._spec.accent.name() == "#a371f7"
    assert v._spec.bg.name() == "#0d1117"
    assert v._spec.sel_text.name() == "#000000"   # same as the old hardcoded selected-row pen


def test_dv3_non_str_skin_id_falls_back(qapp):
    default = SkinSpec()
    for weird in (None, 123, 12.5, True, ["a"], {"x": 1}):
        assert SkinSpec.load(weird).accent.name() == default.accent.name()   # coerces/rejects, never raises


def test_dv3_hostile_spec_still_renders(qapp, tmp_path, monkeypatch):
    """A wrong-type/corrupt skin file must not crash render_native — it falls back to valid colours."""
    from src.ui.qt import device_view as dv

    monkeypatch.setattr(dv, "_SKINS_DIR", tmp_path)
    (tmp_path / "hostile.json").write_text(
        '{"accent": 123, "bg": ["x"], "text": null, "line": "notacolor", "header": {}}', encoding="utf-8")
    v = DeviceView(DeviceScreenModel("ESP32 Marauder", marauder_menu(), skin="hostile"))
    img = v.render_native()                        # must not raise
    assert img.size().width() == 240 and img.size().height() == 320
    assert v._spec.accent.name() == SkinSpec().accent.name()   # per-field fallback held


def test_dv3_load_fallbacks_and_hardening(qapp, tmp_path, monkeypatch):
    from src.ui.qt import device_view as dv

    default = SkinSpec()
    # unknown id + path-traversal / bad ids -> default (never touches the filesystem outside skins/)
    for bad in ("does_not_exist", "../../etc/passwd", "a/b", "..", "", "UPPER", "has space", "x" * 33):
        assert SkinSpec.load(bad).accent.name() == default.accent.name()

    # Point the loader at a temp dir for the malformed-content cases.
    monkeypatch.setattr(dv, "_SKINS_DIR", tmp_path)
    (tmp_path / "badjson.json").write_text("{ not valid json ", encoding="utf-8")
    assert SkinSpec.load("badjson").accent.name() == default.accent.name()

    (tmp_path / "arr.json").write_text("[1, 2, 3]", encoding="utf-8")          # non-object
    assert SkinSpec.load("arr").accent.name() == default.accent.name()

    (tmp_path / "partial.json").write_text('{"accent": "#ff0000"}', encoding="utf-8")
    sp = SkinSpec.load("partial")
    assert sp.accent.name() == "#ff0000"                                       # honored
    assert sp.bg.name() == default.bg.name()                                   # missing key -> per-field default

    (tmp_path / "wrongtype.json").write_text('{"accent": 123, "bg": ["x"], "text": null}', encoding="utf-8")
    sp2 = SkinSpec.load("wrongtype")
    assert sp2.accent.name() == default.accent.name()                          # non-str -> default
    assert sp2.bg.name() == default.bg.name()
    assert sp2.text.name() == default.text.name()

    (tmp_path / "badcolor.json").write_text('{"accent": "notacolor"}', encoding="utf-8")
    assert SkinSpec.load("badcolor").accent.name() == default.accent.name()    # invalid color string -> default


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


# ── DV2: crisp zoom modes (Fit / Integer / 1:1) ──────────────────────
def test_device_view_zoom_modes_scale(qapp):
    v = DeviceView(DeviceScreenModel("ESP32 Marauder", marauder_menu()))
    assert v.zoom_mode == v.ZOOM_FIT                       # default preserves the original Fit behavior
    # 1:1 is exactly native regardless of window size
    v.set_zoom_mode(v.ZOOM_1X)
    assert v._compute_scale(800, 900) == 1.0
    assert v._compute_scale(240, 320) == 1.0
    # Integer is always a whole multiple >= 1, never fractional
    v.set_zoom_mode(v.ZOOM_INTEGER)
    assert v._compute_scale(240, 320) == 1.0               # min size -> 1x
    assert v._compute_scale(720, 960) == 2.0               # room for 2x
    assert v._compute_scale(1200, 1600) == 4.0
    assert v._compute_scale(496, 660) == 2.0               # fit lands EXACTLY on 2.0 -> 2x (no float trunc to 1)
    assert v._compute_scale(736, 976) == 3.0               # exactly 3.0 -> 3x
    for (w, h) in [(300, 400), (517, 763), (999, 640)]:
        s = v._compute_scale(w, h)
        assert s == int(s) and s >= 1                      # whole number, at least 1x
    # Fit is unchanged from the original fractional fill
    v.set_zoom_mode(v.ZOOM_FIT)
    assert v._compute_scale(600, 800) == max(min((600 - 16) / 240, (800 - 16) / 320), 0.1)


def test_device_view_zoom_never_nonpositive_tiny(qapp):
    v = DeviceView(DeviceScreenModel("ESP32 Marauder", marauder_menu()))
    for mode in v.ZOOM_MODES:
        v.set_zoom_mode(mode)
        assert v._compute_scale(10, 10) > 0                 # no zero/negative scale at absurd small sizes


def test_device_view_cycle_and_validate(qapp):
    v = DeviceView(DeviceScreenModel("ESP32 Marauder", marauder_menu()))
    assert v.zoom_mode == v.ZOOM_FIT
    assert v.cycle_zoom() == v.ZOOM_INTEGER
    assert v.cycle_zoom() == v.ZOOM_1X
    assert v.cycle_zoom() == v.ZOOM_FIT                    # wraps back to Fit
    with pytest.raises(ValueError):
        v.set_zoom_mode("huge")


def test_device_view_paint_honors_zoom_and_hittest(qapp):
    """paintEvent must stash the current mode's scale so click->native hit-testing stays correct per mode."""
    v = DeviceView(DeviceScreenModel("ESP32 Marauder", marauder_menu()))
    v.resize(720, 960)                                     # on-ratio (DV1 won't fight it)
    v.show()
    qapp.processEvents()
    try:
        for mode in v.ZOOM_MODES:
            v.set_zoom_mode(mode)
            v.repaint()
            qapp.processEvents()
            assert abs(v._scale - v._compute_scale(v.width(), v.height())) < 1e-9
            assert v._scale > 0
    finally:
        v.close()


def test_device_view_1x_margin_click_is_noop(qapp):
    """A click in the dark margin OUTSIDE the drawn skin (large in 1:1) must not select a row or fire a
    command — no bogus model.enter from clicking empty space."""
    from PyQt5.QtCore import QEvent, QPointF, Qt as _Qt
    from PyQt5.QtGui import QMouseEvent

    fired = []
    v = DeviceView(DeviceScreenModel("ESP32 Marauder", marauder_menu()), send=lambda c: fired.append(c))
    v.set_zoom_mode(v.ZOOM_1X)
    v.resize(600, 800)
    v.show()
    qapp.processEvents()
    v.repaint()
    qapp.processEvents()               # stashes _ox/_oy/_scale
    sel_before = v.model.sel
    ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(2, 2),
                     _Qt.LeftButton, _Qt.LeftButton, _Qt.NoModifier)
    try:
        v.mousePressEvent(ev)
        assert fired == []             # nothing sent
        assert v.model.sel == sel_before
    finally:
        v.close()


def test_render_native_invariant_across_zoom_modes(qapp):
    """render_native() (the golden skin image) is identical regardless of zoom mode — the zoom badge lives
    only in paintEvent, never in the native render."""
    v = DeviceView(DeviceScreenModel("ESP32 Marauder", marauder_menu()))
    base = v.render_native()
    for mode in v.ZOOM_MODES:
        v.set_zoom_mode(mode)
        img = v.render_native()
        assert img.size() == base.size()
        assert img == base             # QImage == is a pixel-wise compare


def test_device_view_1x_is_pixel_exact_crisp(qapp):
    """1:1 draws the skin unscaled and crisp: grabbed widget pixels equal the native render pixel-for-pixel
    (proves no smoothing corrupts the render, and the badge doesn't cover the skin at a normal size)."""
    v = DeviceView(DeviceScreenModel("ESP32 Marauder", marauder_menu()))
    v.set_zoom_mode(v.ZOOM_1X)
    v.resize(400, 500)
    v.show()
    qapp.processEvents()
    v.repaint()
    qapp.processEvents()
    native = v.render_native()
    grabbed = v.grab().toImage()
    ox, oy = v._ox, v._oy
    try:
        for (cx, cy) in [(10, 10), (120, 40), (200, 300), (5, 315)]:
            assert (grabbed.pixel(ox + cx, oy + cy) & 0xFFFFFF) == (native.pixel(cx, cy) & 0xFFFFFF)
    finally:
        v.close()


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
