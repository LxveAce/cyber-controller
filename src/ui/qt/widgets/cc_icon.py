"""Generate CC logo as QIcon for window/taskbar icon.

Matches the sidebar CCLogo widget: two interlocking C arcs
with glow layers and endpoint nodes.
"""

from PyQt5.QtCore import Qt, QRectF, QPointF
from PyQt5.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap
import math

_ACCENT = QColor(57, 255, 20)
_ACCENT_GLOW = QColor(57, 255, 20, 40)
_ACCENT_MID = QColor(57, 255, 20, 120)
_BG = QColor(13, 17, 23)


def create_cc_icon() -> QIcon:
    icon = QIcon()
    for size in [16, 32, 48, 64, 128, 256]:
        icon.addPixmap(_render_cc(size))
    return icon


def _draw_c(p: QPainter, cx: float, cy: float, r: float, pw: float,
            color: QColor, flip: bool = False) -> None:
    pen = QPen(color, pw, Qt.SolidLine, Qt.FlatCap)
    p.setPen(pen)
    rect = QRectF(cx - r, cy - r, r * 2, r * 2)
    if flip:
        p.drawArc(rect, 300 * 16, 240 * 16)
    else:
        p.drawArc(rect, 60 * 16, 240 * 16)


def _draw_nodes(p: QPainter, cx: float, cy: float, r: float,
                node_r: float, color: QColor, flip: bool = False) -> None:
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    ends = [300, 180] if flip else [60, 300]
    for a in ends:
        rad = math.radians(a)
        x = cx + r * math.cos(rad)
        y = cy - r * math.sin(rad)
        p.drawEllipse(QPointF(x, y), node_r, node_r)


def _render_cc(size: int) -> QPixmap:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)

    margin = size * 0.05
    p.setPen(Qt.NoPen)
    p.setBrush(_BG)
    p.drawRoundedRect(QRectF(margin, margin, size - margin * 2, size - margin * 2),
                      size * 0.15, size * 0.15)

    cx = size / 2
    cy = size / 2
    r = size * 0.28
    offset = size * 0.22
    pw = max(1.5, size * 0.06)
    node_r = max(1, size * 0.04)

    lx = cx - offset
    rx = cx + offset

    # Glow layer
    _draw_c(p, lx, cy, r + 2, pw + 4, _ACCENT_GLOW)
    _draw_c(p, rx, cy, r + 2, pw + 4, _ACCENT_GLOW, flip=True)

    # Mid layer
    _draw_c(p, lx, cy, r + 1, pw + 2, _ACCENT_MID)
    _draw_c(p, rx, cy, r + 1, pw + 2, _ACCENT_MID, flip=True)

    # Foreground arcs
    _draw_c(p, lx, cy, r, pw, _ACCENT)
    _draw_c(p, rx, cy, r, pw, _ACCENT, flip=True)

    # Endpoint nodes
    if size >= 32:
        _draw_nodes(p, lx, cy, r, node_r, _ACCENT)
        _draw_nodes(p, rx, cy, r, node_r, _ACCENT, flip=True)

    p.end()
    return QPixmap.fromImage(img)
