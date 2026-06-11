"""UI variant launcher dialog shown when no --ui flag is provided."""

import sys
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont, QColor, QPalette
from PyQt5.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame, QRadioButton, QButtonGroup,
)


class LauncherDialog(QDialog):
    """Dark-themed dialog to select which UI variant to launch."""

    def __init__(self):
        super().__init__()
        self.selected_ui = None
        self.setWindowTitle("Cyber Controller — Select Interface")
        self.setFixedSize(500, 400)
        self._apply_dark_theme()
        self._build_ui()

    def _apply_dark_theme(self):
        pal = self.palette()
        pal.setColor(QPalette.Window, QColor("#0d1117"))
        pal.setColor(QPalette.WindowText, QColor("#e6edf3"))
        pal.setColor(QPalette.Base, QColor("#161b22"))
        pal.setColor(QPalette.Text, QColor("#e6edf3"))
        pal.setColor(QPalette.Button, QColor("#1c2128"))
        pal.setColor(QPalette.ButtonText, QColor("#e6edf3"))
        pal.setColor(QPalette.Highlight, QColor("#39ff14"))
        self.setPalette(pal)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(32, 24, 32, 24)

        # Title
        title = QLabel("CYBER CONTROLLER")
        title.setFont(QFont("JetBrains Mono", 16, QFont.Bold))
        title.setStyleSheet("color: #39ff14;")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("Select your interface")
        subtitle.setFont(QFont("Segoe UI", 10))
        subtitle.setStyleSheet("color: #8b949e;")
        subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(subtitle)

        layout.addSpacing(8)

        # Radio options
        self._group = QButtonGroup(self)

        options = [
            ("qt", "Full GUI (Recommended)",
             "Complete PyQt5 interface with all features, sidebar, persistent terminal, "
             "command palette, and custom widgets. Best for desktop use."),
            ("tk", "Lightweight GUI",
             "Tkinter-based interface with core features. Lower resource usage. "
             "Good for older hardware or when PyQt5 is unavailable."),
            ("tui", "Terminal UI",
             "Textual-based terminal interface. Runs in any terminal emulator. "
             "Ideal for SSH sessions, headless servers, and cyberdeck deployments."),
        ]

        for i, (key, label, desc) in enumerate(options):
            card = QFrame()
            card.setObjectName(f"launcher-card-{i}")
            card.setStyleSheet(
                f"QFrame#launcher-card-{i} {{ background: #161b22; border: 1px solid #30363d; "
                "border-radius: 8px; padding: 12px; }"
                f"QFrame#launcher-card-{i}:hover {{ border-color: #39ff14; }}"
            )
            card_layout = QVBoxLayout(card)

            radio = QRadioButton(label)
            radio.setFont(QFont("Segoe UI", 11, QFont.Bold))
            radio.setStyleSheet(
                "QRadioButton { color: #e6edf3; }"
                "QRadioButton::indicator { width: 16px; height: 16px; }"
            )
            radio.setProperty("ui_key", key)
            if i == 0:
                radio.setChecked(True)
            self._group.addButton(radio, i)
            card_layout.addWidget(radio)

            desc_label = QLabel(desc)
            desc_label.setFont(QFont("Segoe UI", 9))
            desc_label.setStyleSheet("color: #8b949e; margin-left: 24px;")
            desc_label.setWordWrap(True)
            card_layout.addWidget(desc_label)

            layout.addWidget(card)

        layout.addStretch()

        # Launch button
        btn = QPushButton("Launch")
        btn.setFont(QFont("Segoe UI", 11, QFont.Bold))
        btn.setStyleSheet(
            "QPushButton { background: #39ff14; color: #0d1117; border: none; "
            "border-radius: 6px; padding: 10px 32px; font-weight: bold; }"
            "QPushButton:hover { background: #2dd912; }"
        )
        btn.clicked.connect(self._on_launch)
        layout.addWidget(btn, alignment=Qt.AlignCenter)

    def _on_launch(self):
        btn = self._group.checkedButton()
        if btn:
            self.selected_ui = btn.property("ui_key")
        self.accept()


def select_ui() -> str:
    """Show the launcher dialog and return the selected UI key."""
    app = QApplication.instance()
    own_app = False
    if app is None:
        app = QApplication(sys.argv)
        own_app = True

    dialog = LauncherDialog()
    dialog.exec_()
    result = dialog.selected_ui or "qt"

    if own_app:
        # Don't exec the app -- just used it for the dialog
        pass

    return result
