"""Cyber Controller theme engine — QSS-based dark theme with design tokens."""

from pathlib import Path
from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QFont


def apply_theme(app: QApplication) -> None:
    """Apply the cyber-dark QSS stylesheet and base font to the application."""
    qss_path = Path(__file__).parent / "cyber_dark.qss"
    app.setStyleSheet(qss_path.read_text(encoding="utf-8"))
    font = QFont("Segoe UI", 10)
    font.setHintingPreference(QFont.PreferNoHinting)
    app.setFont(font)
