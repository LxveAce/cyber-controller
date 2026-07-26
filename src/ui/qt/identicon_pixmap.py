"""Render a deterministic :class:`~src.core.identicon.Identicon` to a Qt ``QPixmap`` (identity).

Thin view layer over the pure core in :mod:`src.core.identicon`: the pattern + colour are decided
there (headless-testable), this just paints the resolved cell grid as filled rounded squares on a
transparent ground so the same MAC/node shows the SAME little face in its table row, detail panel,
and archive. No state, no I/O — one function.
"""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QPainter, QPixmap

from src.core.identicon import identicon


def identicon_pixmap(key: str, px: int = 28, grid: int = 5) -> QPixmap:
    """A ``px``x``px`` identicon QPixmap for *key* (a MAC / BSSID / node id). Transparent bg, the
    accent colour painting the symmetric cells. Deterministic — same key, same pixmap."""
    ic = identicon(key, grid=grid)
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(*ic.color))
        # A small inset so the face doesn't bleed to the very edge; cells tile the remaining box.
        inset = max(1, px // 12)
        avail = px - 2 * inset
        cell = avail / grid
        radius = max(1.0, cell * 0.18)
        for r, row in enumerate(ic.cells):
            for c, on in enumerate(row):
                if not on:
                    continue
                x = inset + c * cell
                y = inset + r * cell
                painter.drawRoundedRect(
                    round(x), round(y), max(1, round(cell)), max(1, round(cell)), radius, radius)
    finally:
        painter.end()
    return pm
