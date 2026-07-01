"""Device View — an on-screen replica of a firmware's on-board UI.

This is the **RECONSTRUCTED_SKIN** tier (see command-center/projects/cc-device-view-PLAN.md): the ESP32
firmwares render their TFT menu locally and only expose a serial CLI, so we faithfully *rebuild* the menu
in Qt and bind each leaf to the firmware's real serial command. It is honestly a reconstruction, not a
pixel mirror (only Flipper can be a true mirror) — the header carries a "SKIN" tag to say so.

Model-driven, so it runs with NO device attached (canned state) — which is what the marketing demo,
training and kiosk modes need. ``render_native()`` is a pure draw into a QImage (offscreen-testable); the
shared ``paintEvent`` scales that image to whatever window size it's given, aspect-locked, with a bezel.
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt5.QtCore import QRect, QSize, Qt
from PyQt5.QtGui import QColor, QFont, QImage, QPainter
from PyQt5.QtWidgets import QWidget

# ── palette (mirrors cyber_dark.qss) ─────────────────────────────────
_BG = QColor("#0d1117")
_HEAD_BG = QColor("#001a00")
_ACCENT = QColor("#a371f7")
_TEXT = QColor("#e6edf3")
_MUTED = QColor("#8b949e")
_LINE = QColor("#16321a")


class MenuNode:
    """A single menu entry: a submenu (children) or a leaf bound to a serial command."""

    def __init__(self, label: str, *, command: Optional[str] = None,
                 children: "Optional[list[MenuNode]]" = None):
        self.label = label
        self.command = command
        self.children = children or []

    @property
    def is_menu(self) -> bool:
        return bool(self.children)


class DeviceScreenModel:
    """The state a reconstructed skin renders: a firmware title + a navigable menu tree."""

    def __init__(self, title: str, root: "list[MenuNode]", *, status: str = "ready",
                 battery: str = "84%"):
        self.title = title
        self.root = root
        self.status = status
        self.battery = battery
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


# ── a faithful ESP32 Marauder menu (leaves are real Marauder serial commands) ──
def marauder_menu() -> "list[MenuNode]":
    M = MenuNode
    return [
        M("WiFi", children=[
            M("Scan APs", command="scanall"),
            M("Scan Stations", command="scanall"),
            M("Attacks", children=[
                M("Beacon Spam", command="attack -t beacon -r"),
                M("Rick Roll", command="attack -t rickroll"),
                M("Deauth", command="attack -t deauth"),
                M("Probe Flood", command="attack -t probe"),
            ]),
            M("Sniffers", children=[
                M("Beacon Sniff", command="sniffbeacon"),
                M("Deauth Sniff", command="sniffdeauth"),
                M("PMKID", command="sniffpmkid"),
                M("Raw", command="sniffraw"),
            ]),
            M("Channel", command="channel"),
        ]),
        M("Bluetooth", children=[
            M("BLE Scan", command="sniffbt"),
            M("BLE Spam", command="blespam -t all"),
            M("BLE Track", command="sniffbt -t airtag"),
        ]),
        M("Device", children=[
            M("Info", command="info"),
            M("Settings", command="settings"),
            M("Reboot", command="reboot"),
        ]),
    ]


# ── a faithful GhostESP menu (leaves are real GhostESP serial commands) ──
def ghostesp_menu() -> "list[MenuNode]":
    M = MenuNode
    return [
        M("WiFi", children=[
            M("Scan APs", command="scanap"),
            M("Scan Stations", command="scansta"),
            M("Attacks", children=[
                M("Deauth", command="attack -d"),
                M("Beacon Spam", command="beaconspam -r"),
                M("Probe Flood", command="probe"),
                M("Rick Roll", command="beaconspam -rr"),
            ]),
            M("Capture", children=[
                M("Start", command="capture -eapol"),
                M("Stop", command="capture -stop"),
            ]),
            M("Evil Portal", children=[
                M("Start", command="startportal"),
                M("Stop", command="stopportal"),
            ]),
        ]),
        M("Bluetooth", children=[
            M("BLE Scan", command="blescan"),
            M("BLE Track", command="bletrack"),
        ]),
        M("Wardrive", children=[
            M("Start", command="startwd"),
            M("Stop", command="startwd -s"),
        ]),
        M("Device", children=[
            M("Info", command="chipinfo"),
            M("GPS Info", command="gps info"),
            M("SD Info", command="sd info"),
            M("Settings", command="settings"),
            M("Reboot", command="reboot"),
        ]),
    ]


# ── a faithful ESP32-DIV menu (leaves are real ESP32-DIV serial commands) ──
def esp32div_menu() -> "list[MenuNode]":
    M = MenuNode
    return [
        M("WiFi", children=[
            M("Scan APs", command="scanwifi"),
            M("Scan Stations", command="scansta"),
            M("Capture", children=[
                M("Sniff", command="sniff"),
                M("PMKID", command="pmkid"),
                M("Handshake", command="handshake"),
            ]),
            M("Attacks", children=[
                M("Deauth", command="deauth"),
                M("Deauth All", command="deauth all"),
                M("Beacon", command="beacon"),
                M("Rick Roll", command="rickroll"),
            ]),
            M("Channel", command="getch"),
        ]),
        M("Bluetooth", children=[
            M("BLE Scan", command="scanble"),
            M("BLE Spam", command="blespam"),
        ]),
        M("2.4GHz", children=[
            M("NRF Scan", command="nrf scan"),
            M("NRF Sniff", command="nrf sniff"),
            M("NRF Jam", command="nrf jam"),
        ]),
        M("Device", children=[
            M("Info", command="info"),
            M("SD Info", command="sd info"),
            M("Settings", command="settings"),
            M("Reboot", command="reboot"),
        ]),
    ]


# firmware -> (display title, menu factory) for the Device View chooser
SKINS = {
    "marauder": ("ESP32 Marauder", marauder_menu),
    "ghostesp": ("GhostESP", ghostesp_menu),
    "esp32div": ("ESP32-DIV", esp32div_menu),
}


class DeviceView(QWidget):
    """A scaled, bezel-framed reconstruction of a 240x320 TFT firmware UI.

    ``render_native()`` draws the device screen at its real resolution into a QImage (pure — testable with
    no window); ``paintEvent`` scales it into the widget, aspect-locked. Arrow keys / clicks navigate.
    """

    NATIVE = QSize(240, 320)

    def __init__(self, model: DeviceScreenModel, *, send: "Optional[Callable[[str], None]]" = None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.model = model
        self._send = send
        self.setMinimumSize(self.NATIVE)
        self.resize(self.NATIVE.width() * 2, self.NATIVE.height() * 2)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setWindowTitle(f"Device View — {model.title} (reconstructed)")
        self._rows: "list[QRect]" = []  # hit rects in NATIVE coords, filled by render_native

    # ── pure native draw (offscreen-testable) ────────────────────────
    def render_native(self) -> QImage:
        w, h = self.NATIVE.width(), self.NATIVE.height()
        img = QImage(w, h, QImage.Format_ARGB32)
        img.fill(_BG)
        p = QPainter(img)
        try:
            mono = QFont("JetBrains Mono", 9)
            mono.setStyleHint(QFont.Monospace)
            p.setFont(mono)

            # header
            p.fillRect(0, 0, w, 24, _HEAD_BG)
            p.setPen(_ACCENT)
            p.drawLine(0, 24, w, 24)
            p.drawText(QRect(6, 0, w - 70, 24), Qt.AlignVCenter | Qt.AlignLeft, self.model.title)
            p.setFont(QFont("JetBrains Mono", 7))
            p.drawText(QRect(w - 80, 0, 56, 24), Qt.AlignVCenter | Qt.AlignRight, self.model.battery)
            # "SKIN" honesty tag
            p.setPen(_MUTED)
            p.drawText(QRect(w - 24, 0, 22, 24), Qt.AlignVCenter | Qt.AlignRight, "SK")

            # breadcrumb
            p.setFont(QFont("JetBrains Mono", 7))
            p.setPen(_MUTED)
            p.drawText(QRect(6, 26, w - 12, 14), Qt.AlignVCenter | Qt.AlignLeft, self.model.crumb())
            p.setPen(_LINE)
            p.drawLine(0, 40, w, 40)

            # menu list
            p.setFont(mono)
            self._rows = []
            items = self.model.items()
            row_h = 26
            y = 44
            for i, node in enumerate(items):
                r = QRect(0, y, w, row_h)
                self._rows.append(r)
                if i == self.model.sel:
                    p.fillRect(r, _ACCENT)
                    p.setPen(QColor("#000000"))
                else:
                    p.setPen(_TEXT)
                p.drawText(QRect(12, y, w - 24, row_h), Qt.AlignVCenter | Qt.AlignLeft, node.label)
                if node.is_menu:
                    p.drawText(QRect(0, y, w - 12, row_h), Qt.AlignVCenter | Qt.AlignRight, "›")
                y += row_h

            # footer (status)
            p.fillRect(0, h - 18, w, 18, _HEAD_BG)
            p.setPen(_ACCENT)
            p.drawLine(0, h - 18, w, h - 18)
            p.setFont(QFont("JetBrains Mono", 7))
            p.drawText(QRect(6, h - 18, w - 60, 18), Qt.AlignVCenter | Qt.AlignLeft, self.model.status[:34])
            p.drawText(QRect(w - 60, h - 18, 54, 18), Qt.AlignVCenter | Qt.AlignRight, "OK · BACK")
        finally:
            p.end()
        return img

    # ── scale into the window, aspect-locked, with a bezel ───────────
    def paintEvent(self, _ev) -> None:  # noqa: N802 (Qt override)
        img = self.render_native()
        p = QPainter(self)
        try:
            p.fillRect(self.rect(), QColor("#05070a"))
            w, h = self.width(), self.height()
            nw, nh = self.NATIVE.width(), self.NATIVE.height()
            scale = min((w - 16) / nw, (h - 16) / nh)
            scale = max(scale, 0.1)
            dw, dh = int(nw * scale), int(nh * scale)
            x, y = (w - dw) // 2, (h - dh) // 2
            # bezel
            p.setPen(QColor("#222222"))
            p.setBrush(QColor("#0a0a0a"))
            p.drawRoundedRect(x - 7, y - 7, dw + 14, dh + 14, 8, 8)
            p.drawImage(QRect(x, y, dw, dh), img)
            self._scale, self._ox, self._oy = scale, x, y
        finally:
            p.end()

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
