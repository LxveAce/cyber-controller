"""Signal bars delegate — paints 1-4 signal strength bars in table cells."""

from __future__ import annotations

from PyQt5.QtCore import QModelIndex, QRectF, Qt
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem

from src.ui.qt.theme.colors import ACCENT, ERROR, WARNING


class SignalBarsDelegate(QStyledItemDelegate):
    """QStyledItemDelegate that paints signal strength bars for RSSI values.

    Expects the cell's DisplayRole text to be an integer RSSI value (e.g. "-57").
    Renders 1-4 bars colored by threshold plus the dBm text.
    """

    BAR_COUNT = 4
    BAR_GAP = 2
    BAR_WIDTH = 4

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        # Parse RSSI from the cell text
        text = index.data(Qt.DisplayRole) or ""
        try:
            rssi = int(str(text).strip())
        except (ValueError, TypeError):
            # Fall back to default painting if not a number
            painter.restore()
            super().paint(painter, option, index)
            return

        # Determine bar count and color from RSSI
        if rssi > -50:
            bars = 4
            color = QColor(ACCENT)
        elif rssi > -65:
            bars = 3
            color = QColor(ACCENT)
        elif rssi > -75:
            bars = 2
            color = QColor(WARNING)
        else:
            bars = 1
            color = QColor(ERROR)

        inactive_color = QColor("#2d333b")

        rect = option.rect
        y_bottom = rect.bottom() - 4
        x_start = rect.left() + 8
        max_height = rect.height() - 12

        # Draw selection background if selected
        if option.state & 0x00000008:  # State_Selected
            painter.fillRect(rect, QColor("#1c2128"))

        # Draw bars
        for i in range(self.BAR_COUNT):
            bar_height = int(max_height * (i + 1) / self.BAR_COUNT)
            bar_x = x_start + i * (self.BAR_WIDTH + self.BAR_GAP)
            bar_rect = QRectF(
                bar_x,
                y_bottom - bar_height,
                self.BAR_WIDTH,
                bar_height,
            )
            if i < bars:
                painter.fillRect(bar_rect, color)
            else:
                painter.fillRect(bar_rect, inactive_color)

        # Draw dBm text to the right of bars
        text_x = x_start + self.BAR_COUNT * (self.BAR_WIDTH + self.BAR_GAP) + 4
        text_rect = QRectF(text_x, rect.top(), rect.width() - text_x + rect.left(), rect.height())
        painter.setPen(color)
        from PyQt5.QtGui import QFont
        painter.setFont(QFont("JetBrains Mono", 8))
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, f"{rssi} dBm")

        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex):
        hint = super().sizeHint(option, index)
        hint.setWidth(max(hint.width(), 100))
        return hint
