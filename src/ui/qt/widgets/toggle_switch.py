"""Toggle switch widget — animated on/off toggle with accent coloring."""

from __future__ import annotations

from PyQt5.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    Qt,
    pyqtProperty,
    pyqtSignal,
)
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import QWidget

from src.ui.qt.theme.colors import ACCENT, TEXT_DISABLED


class ToggleSwitch(QWidget):
    """Animated toggle switch widget.

    Emits ``toggled(bool)`` when the checked state changes.
    """

    toggled = pyqtSignal(bool)

    def __init__(self, checked: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._checked = checked
        self._knob_x = 22.0 if checked else 2.0
        self.setFixedSize(40, 20)
        self.setCursor(Qt.PointingHandCursor)

        self._anim = QPropertyAnimation(self, b"knob_position", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.InOutCubic)

    @property
    def checked(self) -> bool:
        return self._checked

    @checked.setter
    def checked(self, state: bool) -> None:
        if state == self._checked:
            return
        self._checked = state
        target = 22.0 if state else 2.0
        self._anim.stop()
        self._anim.setStartValue(self._knob_x)
        self._anim.setEndValue(target)
        self._anim.start()
        self.toggled.emit(state)

    def set_checked(self, state: bool) -> None:
        """Convenience setter for use with signals."""
        self.checked = state

    # Qt property for animation
    @pyqtProperty(float)
    def knob_position(self) -> float:
        return self._knob_x

    @knob_position.setter  # type: ignore[no-redef]
    def knob_position(self, x: float) -> None:
        self._knob_x = x
        self.update()

    def mousePressEvent(self, _event) -> None:
        self.checked = not self._checked

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()

        # Track
        track_color = QColor(ACCENT) if self._checked else QColor(TEXT_DISABLED)
        painter.setBrush(track_color)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)

        # Knob
        knob_diameter = h - 4
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(QRectF(self._knob_x, 2, knob_diameter, knob_diameter))

        painter.end()
