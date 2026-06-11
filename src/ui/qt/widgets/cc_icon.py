"""Generate CC logo as QIcon for window/taskbar icon."""

from PyQt5.QtCore import Qt, QRectF, QPointF, QSize
from PyQt5.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPen, QPixmap
import math

_ACCENT = QColor(57, 255, 20)
_BG = QColor(13, 17, 23)


def create_cc_icon() -> QIcon:
    """Create a multi-size QIcon with the CC logo."""
    icon = QIcon()
    for size in [16, 32, 48, 64, 128, 256]:
        pixmap = _render_cc(size)
        icon.addPixmap(pixmap)
    return icon


def _render_cc(size: int) -> QPixmap:
    """Render the CC logo at a given pixel size."""
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)

    # Background circle
    p.setPen(Qt.NoPen)
    p.setBrush(_BG)
    margin = size * 0.05
    p.drawRoundedRect(QRectF(margin, margin, size - margin*2, size - margin*2), size*0.15, size*0.15)

    # Draw two C arcs
    cx = size / 2
    cy = size / 2
    r = size * 0.3
    pw = max(1.5, size * 0.08)
    offset = size * 0.12

    pen = QPen(_ACCENT, pw, Qt.SolidLine, Qt.FlatCap)
    p.setPen(pen)

    # Left C
    rect_l = QRectF(cx - offset - r, cy - r, r*2, r*2)
    p.drawArc(rect_l, 60*16, 240*16)

    # Right C (mirrored)
    rect_r = QRectF(cx + offset - r, cy - r, r*2, r*2)
    p.drawArc(rect_r, 300*16, 240*16)

    # Circuit ticks
    tick_pen = QPen(_ACCENT, max(1, pw * 0.4), Qt.SolidLine, Qt.FlatCap)
    p.setPen(tick_pen)
    tick_len = size * 0.05

    for c_cx, angles in [(cx - offset, [120, 180, 240]), (cx + offset, [0, 300, 360])]:
        for a in angles:
            rad = math.radians(a)
            x0 = c_cx + r * math.cos(rad)
            y0 = cy - r * math.sin(rad)
            x1 = c_cx + (r + tick_len) * math.cos(rad)
            y1 = cy - (r + tick_len) * math.sin(rad)
            p.drawLine(QPointF(x0, y0), QPointF(x1, y1))

    p.end()
    return QPixmap.fromImage(img)
