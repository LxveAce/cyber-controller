"""Device View — an on-screen replica of a firmware's on-board UI.

This is the **RECONSTRUCTED_SKIN** tier (see the internal device-view notes): the ESP32
firmwares render their TFT menu locally and only expose a serial CLI, so we faithfully *rebuild* the menu
in Qt and bind each leaf to the firmware's real serial command. It is honestly a reconstruction, not a
pixel mirror (only Flipper can be a true mirror) — the header carries a "SKIN" tag to say so.

Model-driven, so it runs with NO device attached (canned state) — which is what the marketing demo,
training and kiosk modes need. ``render_native()`` is a pure draw into a QImage (offscreen-testable); the
shared ``paintEvent`` scales that image to whatever window size it's given, aspect-locked, with a bezel.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Optional

from PyQt5.QtCore import QRect, QSize, Qt
from PyQt5.QtGui import QColor, QFont, QImage, QPainter
from PyQt5.QtWidgets import QWidget

from src.core.resources import resource_path
# The menu model was extracted to a pure (Qt-free) module so the web Device View reuses the exact same
# reconstruction. Re-exported here so existing importers (cardputer_remote, main_window, tests) are unchanged.
from src.core.device_menus import (  # noqa: F401
    SKINS,
    MenuNode,
    bruce_menu,
    esp32div_serial_menu,
    esp32div_stock_menu,
    ghostesp_menu,
    marauder_menu,
)

# ── palette (the built-in DEFAULT; a per-firmware SkinSpec overrides it — see skins/*.json) ──────────
_BG = QColor("#0d1117")
_HEAD_BG = QColor("#001a00")
_ACCENT = QColor("#a371f7")
_TEXT = QColor("#e6edf3")
_MUTED = QColor("#8b949e")
_LINE = QColor("#16321a")

# Resolve via resource_path (NOT __file__) so the JSON is found in a frozen build's _MEIPASS too — see
# src/core/resources.py; build.py must --add-data this dir (mirrors the profiles/theme bundling).
_SKINS_DIR = resource_path("src", "ui", "qt", "skins")

# The colour ROLES a skin defines; the values here are the built-in default (the original violet palette),
# used as a whole-spec AND per-field fallback so a missing/partial/corrupt JSON can never break rendering.
_DEFAULT_PALETTE = {
    "bg": _BG.name(),
    "header": _HEAD_BG.name(),
    "accent": _ACCENT.name(),
    "text": _TEXT.name(),
    "muted": _MUTED.name(),
    "line": _LINE.name(),
    "sel_text": "#000000",   # text drawn ON the accent-filled selected row
}


class SkinSpec:
    """A per-firmware colour palette for the reconstructed Device-View skin (DV3).

    Loaded from ``skins/<skin_id>.json``; every field falls back to the built-in default, and load() is
    hardened against a bad skin id (path traversal) and malformed/partial JSON — it NEVER raises, so a
    broken skin file degrades to the default look instead of crashing the view.
    """

    _FIELDS = tuple(_DEFAULT_PALETTE)

    def __init__(self, **colors: object) -> None:
        for name in self._FIELDS:
            val = colors.get(name)
            c = QColor(val) if isinstance(val, str) else QColor()
            if not c.isValid():
                c = QColor(_DEFAULT_PALETTE[name])   # per-field fallback for a missing/invalid colour
            setattr(self, name, c)

    @classmethod
    def from_dict(cls, data: object) -> "SkinSpec":
        if not isinstance(data, dict):
            return cls()
        return cls(**{k: data.get(k) for k in cls._FIELDS})

    @classmethod
    def load(cls, skin_id: str) -> "SkinSpec":
        # Only a bare, lowercase id is allowed — this both namespaces the file and blocks path traversal
        # (no '/', '\\', '.', '..'). Anything else -> the default spec.
        if not skin_id or not re.fullmatch(r"[a-z0-9_]{1,32}", str(skin_id)):
            return cls()
        path = _SKINS_DIR / f"{skin_id}.json"
        if not path.is_file():
            return cls()
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 — a corrupt skin file must never break the view
            return cls()


class DeviceScreenModel:
    """The state a reconstructed skin renders: a firmware title + a navigable menu tree."""

    def __init__(self, title: str, root: "list[MenuNode]", *, status: str = "ready",
                 battery: str = "84%", skin: str = "", native_size: "Optional[QSize]" = None):
        self.title = title
        self.root = root
        self.status = status
        self.battery = battery
        self.skin = skin          # skin id (a SKINS key) -> selects the per-firmware SkinSpec
        # CP1: the board's real TFT resolution (e.g. Cardputer 240x135 landscape, M5StickC 135x240). None ->
        # the DeviceView default (240x320). The View resolves + validates this into self._native.
        self.native_size = native_size
        self.path: "list[int]" = []   # indices into nested menus
        self.sel = 0

    # ── navigation ───────────────────────────────────────────────────
    def items(self) -> "list[MenuNode]":
        items = self.root
        for i in self.path:
            items = items[i].children
        return items

    def crumb(self) -> str:
        node = self.root
        parts = []
        for i in self.path:
            parts.append(node[i].label)
            node = node[i].children
        return " › ".join(parts) if parts else "Main Menu"

    def down(self) -> None:
        n = len(self.items())
        if n:
            self.sel = (self.sel + 1) % n

    def up(self) -> None:
        n = len(self.items())
        if n:
            self.sel = (self.sel - 1) % n

    def enter(self, send: "Optional[Callable[[str], None]]" = None) -> None:
        items = self.items()
        if not items:
            return
        node = items[self.sel]
        if node.is_menu:
            self.path.append(self.sel)
            self.sel = 0
        elif node.needs_arg:
            # Honest: this real command needs an argument we can't supply from a bare menu button, so we
            # surface that instead of firing a broken command (no arg-entry affordance yet — DV4 follow-up).
            self.status = "needs arg: " + (node.command or node.label)
        elif node.command is not None:
            sent = False
            if send is not None:
                try:
                    sent = bool(send(node.command))
                except Exception:  # noqa: BLE001 — a send failure must never break menu navigation
                    sent = False
            # Honest status: "sent" only if it actually went to a device; otherwise it's a preview.
            self.status = ("» sent: " if sent else "preview: ") + node.command

    def back(self) -> None:
        if self.path:
            self.path.pop()
            self.sel = 0


# CP1: real on-board TFT resolutions, for building a board-shaped DeviceScreenModel(..., native_size=...).
# Sources: M5Cardputer LCD = 240x135 (landscape, ST7789); M5StickC/StickC-Plus = 135x240 (portrait);
# the generic 2.4"/2.8" CYD-class TFT = 240x320 (the DeviceView default). Honest reconstructions, not mirrors.
BOARD_SIZES = {
    "cardputer": QSize(240, 135),
    "m5stickc": QSize(135, 240),
    "tft_240x320": QSize(240, 320),
}


class DeviceView(QWidget):
    """A scaled, bezel-framed reconstruction of a 240x320 TFT firmware UI.

    ``render_native()`` draws the device screen at its real resolution into a QImage (pure — testable with
    no window); ``paintEvent`` scales it into the widget, aspect-locked. Arrow keys / clicks navigate.
    """

    NATIVE = QSize(240, 320)

    # DV2 zoom modes — how the 240x320 render is SCALED inside the (DV1 aspect-locked) window.
    ZOOM_FIT = "fit"          # scale to fill, aspect-locked (default; smooth) — the original behavior
    ZOOM_INTEGER = "integer"  # largest whole-pixel multiple that fits (>=1x), crisp (no smoothing)
    ZOOM_1X = "1:1"           # exactly native 240x320, centered, crisp
    ZOOM_MODES = (ZOOM_FIT, ZOOM_INTEGER, ZOOM_1X)

    def __init__(self, model: DeviceScreenModel, *, send: "Optional[Callable[[str], None]]" = None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.model = model
        self._spec = SkinSpec.load(getattr(model, "skin", ""))   # DV3: per-firmware palette (safe fallback)
        # CP1: resolve the board's native resolution (per-model), falling back to the class default. Everything
        # below (min size, aspect-lock, zoom scale, hit-test) reads self._native so a landscape board stays
        # coherent. Guard against a missing/degenerate size so render/scale never divide by zero.
        ns = getattr(model, "native_size", None)
        # isinstance guard (matches the module's defensive style): a missing/non-QSize/degenerate size falls
        # back to the default rather than raising, so render/scale never divide by zero.
        self._native = ns if (isinstance(ns, QSize) and ns.width() > 0 and ns.height() > 0) else self.NATIVE
        self._send = send
        # Set the re-entrancy guard BEFORE any resize() — a synchronous resizeEvent during construction would
        # otherwise read an unset attribute. Advertise the native ratio via the size policy so a future
        # aspect-ENFORCING container (DV6) can use it; note a plain box layout treats heightForWidth as a
        # preferred height only, not a hard constraint, so the self-snap below is what kills the bandaid today.
        self._aspect_locking = False
        sp = self.sizePolicy()
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.setMinimumSize(self._native)
        self.resize(self._native.width() * 2, self._native.height() * 2)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setWindowTitle(f"Device View — {model.title} (reconstructed)")
        self._rows: "list[QRect]" = []  # hit rects in native coords, filled by render_native
        self._zoom_mode = self.ZOOM_FIT

    # ── aspect-ratio contract (DV1: kill the resize-bandaid letterbox) ────
    def hasHeightForWidth(self) -> bool:  # noqa: N802 (Qt override)
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 (Qt override)
        n = self._native
        return round(width * n.height() / n.width())

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        w = self._native.width() * 2
        return QSize(w, self.heightForWidth(w))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        return QSize(self._native.width(), self._native.height())

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._lock_aspect()

    def _lock_aspect(self) -> None:
        """Snap our own geometry to the native aspect when we ARE a top-level window.

        Qt honors ``heightForWidth`` only for widgets INSIDE a layout, never for a bare top-level window —
        and the Device View is shown as its own window — so a free drag would letterbox dead-space around the
        bezel (the "resize bandaid"). We anchor to width and follow height. Guarded against the resize()->
        resizeEvent recursion; a no-op (<=1px) when already on-ratio so it converges in one snap."""
        if self._aspect_locking or not self.isWindow():
            return  # inside a layout, the size policy's heightForWidth handles it
        if self.isMaximized() or self.isFullScreen():
            # Never fight the WM's maximize/fullscreen geometry: snapping to the fixed board aspect would force
            # an off-screen dimension (a portrait board too tall, a landscape board too narrow) and thrash.
            return
        w = self.width()
        target_h = self.heightForWidth(w)
        if abs(self.height() - target_h) <= 1:
            return
        self._aspect_locking = True
        try:
            self.resize(w, target_h)
        finally:
            self._aspect_locking = False

    # ── pure native draw (offscreen-testable) ────────────────────────
    def render_native(self) -> QImage:
        w, h = self._native.width(), self._native.height()
        spec = self._spec   # DV3: per-firmware palette (see skins/*.json)
        img = QImage(w, h, QImage.Format_ARGB32)
        img.fill(spec.bg)
        p = QPainter(img)
        try:
            mono = QFont("JetBrains Mono", 9)
            mono.setStyleHint(QFont.Monospace)
            p.setFont(mono)

            # header
            p.fillRect(0, 0, w, 24, spec.header)
            p.setPen(spec.accent)
            p.drawLine(0, 24, w, 24)
            p.drawText(QRect(6, 0, w - 70, 24), Qt.AlignVCenter | Qt.AlignLeft, self.model.title)
            p.setFont(QFont("JetBrains Mono", 7))
            p.drawText(QRect(w - 80, 0, 56, 24), Qt.AlignVCenter | Qt.AlignRight, self.model.battery)
            # "SKIN" honesty tag
            p.setPen(spec.muted)
            p.drawText(QRect(w - 24, 0, 22, 24), Qt.AlignVCenter | Qt.AlignRight, "SK")

            # breadcrumb
            p.setFont(QFont("JetBrains Mono", 7))
            p.setPen(spec.muted)
            p.drawText(QRect(6, 26, w - 12, 14), Qt.AlignVCenter | Qt.AlignLeft, self.model.crumb())
            p.setPen(spec.line)
            p.drawLine(0, 40, w, 40)

            # menu list
            p.setFont(mono)
            self._rows = []
            items = self.model.items()
            row_h = 26
            y = 44
            footer_top = h - 18   # CP1: on a short board (e.g. Cardputer 240x135) stop before the footer so
            for i, node in enumerate(items):        # we never DRAW or register a hit-rect for a row hidden
                if y + row_h > footer_top:          # under it — a click there must not fire an off-canvas row.
                    break
                r = QRect(0, y, w, row_h)
                self._rows.append(r)
                if i == self.model.sel:
                    p.fillRect(r, spec.accent)
                    p.setPen(spec.sel_text)
                else:
                    p.setPen(spec.text)
                p.drawText(QRect(12, y, w - 24, row_h), Qt.AlignVCenter | Qt.AlignLeft, node.label)
                if node.is_menu:
                    p.drawText(QRect(0, y, w - 12, row_h), Qt.AlignVCenter | Qt.AlignRight, "›")
                y += row_h

            # footer (status)
            p.fillRect(0, h - 18, w, 18, spec.header)
            p.setPen(spec.accent)
            p.drawLine(0, h - 18, w, h - 18)
            p.setFont(QFont("JetBrains Mono", 7))
            p.drawText(QRect(6, h - 18, w - 60, 18), Qt.AlignVCenter | Qt.AlignLeft, self.model.status[:34])
            p.drawText(QRect(w - 60, h - 18, 54, 18), Qt.AlignVCenter | Qt.AlignRight, "OK · BACK")
        finally:
            p.end()
        return img

    # ── scale into the window, aspect-locked, with a bezel ───────────
    # ── DV2 zoom (crisp integer / 1:1 / fit) ─────────────────────────
    @property
    def zoom_mode(self) -> str:
        return self._zoom_mode

    def set_zoom_mode(self, mode: str) -> None:
        if mode not in self.ZOOM_MODES:
            raise ValueError(f"unknown zoom mode {mode!r}; expected one of {self.ZOOM_MODES}")
        self._zoom_mode = mode
        self.update()

    def cycle_zoom(self) -> str:
        """Rotate Fit -> Integer -> 1:1 -> Fit (bound to the Z key). Returns the new mode."""
        i = self.ZOOM_MODES.index(self._zoom_mode)
        self._zoom_mode = self.ZOOM_MODES[(i + 1) % len(self.ZOOM_MODES)]
        self.update()
        return self._zoom_mode

    def _compute_scale(self, w: int, h: int) -> float:
        """Scale factor for the current zoom mode. Fit = fractional fill (original behavior); Integer =
        largest whole-pixel multiple that fits (>=1, crisp); 1:1 = exactly native. Never <=0 (guards a
        window dragged smaller than the bezel inset)."""
        nw, nh = self._native.width(), self._native.height()
        fit = min((w - 16) / nw, (h - 16) / nh)
        if self._zoom_mode == self.ZOOM_1X:
            return 1.0
        if self._zoom_mode == self.ZOOM_INTEGER:
            return float(max(1, int(fit)))       # floor to a whole multiple, at least 1x
        return max(fit, 0.1)                       # Fit — clamped off zero

    def paintEvent(self, _ev) -> None:  # noqa: N802 (Qt override)
        img = self.render_native()
        p = QPainter(self)
        try:
            p.fillRect(self.rect(), QColor("#05070a"))
            w, h = self.width(), self.height()
            nw, nh = self._native.width(), self._native.height()
            scale = self._compute_scale(w, h)
            # Nearest-neighbor for EVERY mode: crisp integer doubling, higher fidelity to the real (pixel)
            # TFT, and it preserves the pre-DV2 Fit rendering exactly (Qt's default was already False — a
            # per-mode 'smooth Fit' would have silently blurred the default view).
            p.setRenderHint(QPainter.SmoothPixmapTransform, False)
            dw, dh = int(nw * scale), int(nh * scale)
            x, y = (w - dw) // 2, (h - dh) // 2
            # bezel
            p.setPen(QColor("#222222"))
            p.setBrush(QColor("#0a0a0a"))
            p.drawRoundedRect(x - 7, y - 7, dw + 14, dh + 14, 8, 8)
            p.drawImage(QRect(x, y, dw, dh), img)
            self._scale, self._ox, self._oy = scale, x, y
            self._paint_zoom_badge(p, w, h, y + dh)
        finally:
            p.end()

    def _paint_zoom_badge(self, p: QPainter, w: int, h: int, img_bottom: int) -> None:
        # Subtle discoverability hint for the Z-cycle, drawn in the dark margin BELOW the skin (never over
        # it) and only in paintEvent — NOT render_native — so the offscreen golden renders stay byte-stable.
        if h - 18 <= img_bottom + 2:
            return  # no clear margin below the skin — skip rather than overpaint the device footer
        p.setPen(QColor("#3fb950"))
        badge = QFont("JetBrains Mono", 7)
        badge.setStyleHint(QFont.Monospace)
        p.setFont(badge)
        p.drawText(QRect(6, h - 18, w - 12, 14), Qt.AlignLeft | Qt.AlignVCenter,
                   f"{self._zoom_mode} · {self._scale:.2f}x · [Z]")

    # ── input ────────────────────────────────────────────────────────
    def keyPressEvent(self, ev) -> None:  # noqa: N802
        k = ev.key()
        if k in (Qt.Key_Down, Qt.Key_S):
            self.model.down()
        elif k in (Qt.Key_Up, Qt.Key_W):
            self.model.up()
        elif k in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Right, Qt.Key_D):
            self.model.enter(self._send)
        elif k in (Qt.Key_Backspace, Qt.Key_Left, Qt.Key_A, Qt.Key_Escape):
            self.model.back()
        elif k == Qt.Key_Z:
            self.cycle_zoom()          # DV2: crisp-zoom mode cycle (Fit -> Integer -> 1:1)
        else:
            return super().keyPressEvent(ev)
        self.update()

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        ox, oy, scale = getattr(self, "_ox", 0), getattr(self, "_oy", 0), getattr(self, "_scale", 1)
        nx = (ev.x() - ox) / scale
        ny = (ev.y() - oy) / scale
        for i, r in enumerate(self._rows):
            if r.contains(int(nx), int(ny)):
                self.model.sel = i
                self.model.enter(self._send)
                self.update()
                return
