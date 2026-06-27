"""Cyber Controller theme engine - QSS-based dark theme with design tokens."""

import logging

from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QFont

from src.core.resources import resource_path

log = logging.getLogger(__name__)


def apply_theme(app: QApplication) -> None:
    """Apply the cyber-dark QSS stylesheet and base font to the application.

    Frozen-safe: the stylesheet is resolved via :func:`resource_path` so it is found
    inside a PyInstaller bundle, and a missing/unreadable stylesheet degrades to the
    default Qt style (logged) instead of crashing the GUI at startup.
    """
    qss_path = resource_path("src", "ui", "qt", "theme", "cyber_dark.qss")
    try:
        app.setStyleSheet(qss_path.read_text(encoding="utf-8"))
    except OSError as exc:
        log.warning("Theme stylesheet unavailable (%s) - using default Qt style: %s", qss_path, exc)
    font = QFont("Segoe UI", 10)
    font.setHintingPreference(QFont.PreferNoHinting)
    app.setFont(font)
