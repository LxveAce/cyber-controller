"""CC Logo widget — stylized QPainter-drawn interlocking 'CC' logo with glow effect."""

from __future__ import annotations

from PyQt5.QtCore import Qt, QRectF, QPointF
from PyQt5.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QRadialGradient
from PyQt5.QtWidgets import QWidget


_ACCENT = "#39ff14"
_ACCENT_GLOW = QColor(57, 255, 20, 40)
_ACCENT_MID = QColor(57, 255, 20, 120)


class CCLogo(QWidget):
    """Two interlocking 'C' letters with glow layers and endpoint nodes.

    Designed for sidebar placement at approximately 180x60.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(180, 60)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    # ── Painting ────────────────────────────────────────────────────

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()

        # The two C letters occupy the upper portion; text sits below.
        letter_h = h - 18
        cx = w / 2
        cy = letter_h / 2

        # --- Glow layer (larger, blurred arcs behind) ---
        self._draw_c(painter, cx - 11, cy, radius=18, pen_width=7, color=_ACCENT_GLOW)
        self._draw_c(painter, cx + 11, cy, radius=18, pen_width=7, color=_ACCENT_GLOW, flip=True)

        # Secondary mid-opacity layer for depth
        self._draw_c(painter, cx - 11, cy, radius=16, pen_width=5, color=_ACCENT_MID)
        self._draw_c(painter, cx + 11, cy, radius=16, pen_width=5, color=_ACCENT_MID, flip=True)

        # --- Foreground C letters ---
        accent = QColor(_ACCENT)
        self._draw_c(painter, cx - 11, cy, radius=14, pen_width=3, color=accent)
        self._draw_c(painter, cx + 11, cy, radius=14, pen_width=3, color=accent, flip=True)

        # --- Gear teeth ---
        tooth_color = QColor(57, 255, 20, 160)
        self._draw_teeth(painter, cx - 11, cy, radius=14, tooth_len=3, color=tooth_color)
        self._draw_teeth(painter, cx + 11, cy, radius=14, tooth_len=3, color=tooth_color, flip=True)

        # --- Endpoint nodes ---
        node_color = QColor(_ACCENT)
        self._draw_nodes(painter, cx - 11, cy, radius=14, color=node_color)
        self._draw_nodes(painter, cx + 11, cy, radius=14, color=node_color, flip=True)

        # --- "CYBER CONTROLLER" text ---
        font = QFont("JetBrains Mono", 6)
        font.setLetterSpacing(QFont.AbsoluteSpacing, 1.6)
        painter.setFont(font)
        painter.setPen(QColor(_ACCENT))
        painter.drawText(QRectF(0, h - 16, w, 16), Qt.AlignCenter, "CYBER CONTROLLER")

        painter.end()

    # ── Helper: draw a single C arc ─────────────────────────────────

    @staticmethod
    def _draw_c(
        painter: QPainter,
        cx: float,
        cy: float,
        radius: float,
        pen_width: float,
        color: QColor,
        flip: bool = False,
    ) -> None:
        """Draw a 'C' shaped arc (approx 240 degrees open on the right or left)."""
        pen = QPen(color, pen_width, Qt.SolidLine, Qt.FlatCap)
        painter.setPen(pen)

        rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)

        if flip:
            # Mirrored C: opening faces left; start at ~300 deg, sweep 240 clockwise
            start_angle = 300 * 16
            span_angle = 240 * 16
        else:
            # Normal C: opening faces right; start at ~60 deg, sweep 240 counter-clockwise
            start_angle = 60 * 16
            span_angle = 240 * 16

        painter.drawArc(rect, start_angle, span_angle)

    # ── Helper: gear teeth ───────────────────────────────────────

    @staticmethod
    def _draw_teeth(
        painter: QPainter,
        cx: float,
        cy: float,
        radius: float,
        tooth_len: float,
        color: QColor,
        flip: bool = False,
    ) -> None:
        import math

        pen = QPen(color, 1.2, Qt.SolidLine, Qt.FlatCap)
        painter.setPen(pen)
        angles = [300, 330, 0, 30, 60, 90, 120, 150, 180] if flip else [60, 90, 120, 150, 180, 210, 240, 270, 300]
        for a in angles:
            rad = math.radians(a)
            x1 = cx + radius * math.cos(rad)
            y1 = cy - radius * math.sin(rad)
            x2 = cx + (radius + tooth_len) * math.cos(rad)
            y2 = cy - (radius + tooth_len) * math.sin(rad)
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    # ── Helper: endpoint nodes ─────────────────────────────────────

    @staticmethod
    def _draw_nodes(
        painter: QPainter,
        cx: float,
        cy: float,
        radius: float,
        color: QColor,
        flip: bool = False,
    ) -> None:
        """Draw small filled circles at the open ends of the C arc."""
        import math

        painter.setPen(Qt.NoPen)
        painter.setBrush(color)

        if flip:
            ends = [300, 180]
        else:
            ends = [60, 300]

        for a in ends:
            rad = math.radians(a)
            x = cx + radius * math.cos(rad)
            y = cy - radius * math.sin(rad)
            painter.drawEllipse(QPointF(x, y), 2.5, 2.5)
