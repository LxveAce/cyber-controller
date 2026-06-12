"""Arc gauge widget — 270-degree radial gauge with threshold coloring."""

from __future__ import annotations

import math

from PyQt5.QtCore import Qt, QRectF
from PyQt5.QtGui import QColor, QFont, QPainter, QPen
from PyQt5.QtWidgets import QWidget

from src.ui.qt.theme.colors import ACCENT, BG_INPUT, WARNING, ERROR


class ArcGauge(QWidget):
    """A 270-degree arc gauge with centered value text and a label below.

    Args:
        value: Gauge value 0-100.
        label: Text shown below the gauge (e.g. "CPU").
        color_override: If set, bypass threshold coloring.
        parent: Parent widget.
    """

    def __init__(
        self,
        value: int = 0,
        label: str = "",
        color_override: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._value = max(0, min(100, value))
        self._label = label
        self._color_override = color_override
        self.setMinimumSize(120, 140)

    @property
    def value(self) -> int:
        return self._value

    @value.setter
    def value(self, v: int) -> None:
        self._value = max(0, min(100, v))
        self.update()

    @property
    def label(self) -> str:
        return self._label

    @label.setter
    def label(self, text: str) -> None:
        self._label = text
        self.update()

    def set_value(self, v: int) -> None:
        """Convenience setter for use with signals."""
        self.value = v

    def set_label(self, text: str) -> None:
        """Convenience setter for use with signals."""
        self.label = text

    def _arc_color(self) -> QColor:
        if self._color_override:
            return QColor(self._color_override)
        if self._value >= 90:
            return QColor(ERROR)
        if self._value >= 80:
            return QColor(WARNING)
        if self._value >= 60:
            return QColor("#ffd700")
        return QColor(ACCENT)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        dpr = self.devicePixelRatioF()
        w = self.width()
        h = self.height()
        side = min(w, h - 24)  # leave room for label text
        cx = w / 2
        cy = (h - 24) / 2

        arc_width = max(6, side * 0.12)
        radius = (side - arc_width) / 2 - 4

        arc_rect = QRectF(
            cx - radius,
            cy - radius,
            radius * 2,
            radius * 2,
        )

        # 270-degree arc: starts at 225 degrees (bottom-left), sweeps 270 degrees clockwise
        start_angle = 225 * 16  # Qt uses 1/16th degrees
        span_angle = -270 * 16  # negative = clockwise

        # Background arc
        bg_pen = QPen(QColor(BG_INPUT), arc_width, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(bg_pen)
        painter.drawArc(arc_rect, start_angle, span_angle)

        # Value arc
        value_span = int(span_angle * self._value / 100)
        if value_span != 0:
            fg_pen = QPen(self._arc_color(), arc_width, Qt.SolidLine, Qt.RoundCap)
            painter.setPen(fg_pen)
            painter.drawArc(arc_rect, start_angle, value_span)

        # Center value text
        painter.setPen(Qt.NoPen)
        value_font = QFont("JetBrains Mono", max(12, int(side * 0.22)), QFont.Bold)
        painter.setFont(value_font)
        painter.setPen(QColor("#e6edf3"))
        painter.drawText(
            QRectF(cx - radius, cy - radius * 0.4, radius * 2, radius * 0.8),
            Qt.AlignCenter,
            f"{self._value}%",
        )

        # Label text below gauge
        if self._label:
            label_font = QFont("Segoe UI", max(8, int(side * 0.1)))
            painter.setFont(label_font)
            painter.setPen(QColor("#8b949e"))
            painter.drawText(
                QRectF(0, h - 24, w, 24),
                Qt.AlignCenter,
                self._label,
            )

        painter.end()
