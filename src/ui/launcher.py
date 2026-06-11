"""UI variant launcher dialog shown when no --ui flag is provided."""

import sys
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication, QDialog, QVBoxLayout,
    QLabel, QPushButton, QFrame, QRadioButton, QButtonGroup,
)

_LAUNCHER_QSS = """
QDialog {
    background: #0d1117;
}
QLabel {
    color: #e6edf3;
    background: transparent;
}
QFrame#launcher-card {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
}
QFrame#launcher-card:hover {
    border-color: #39ff14;
}
QRadioButton {
    color: #e6edf3;
    background: transparent;
    spacing: 8px;
}
QRadioButton::indicator {
    width: 16px;
    height: 16px;
    border: 2px solid #30363d;
    border-radius: 9px;
    background: #0d1117;
}
QRadioButton::indicator:checked {
    background: #39ff14;
    border-color: #39ff14;
}
QRadioButton::indicator:hover {
    border-color: #39ff14;
}
"""


class LauncherDialog(QDialog):
    """Dark-themed dialog to select which UI variant to launch."""

    def __init__(self):
        super().__init__()
        self.selected_ui = None
        self.setWindowTitle("Cyber Controller — Select Interface")
        self.setFixedSize(480, 380)
        self.setWindowFlags(
            Qt.Dialog
            | Qt.WindowTitleHint
            | Qt.WindowCloseButtonHint
        )
        self.setStyleSheet(_LAUNCHER_QSS)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(32, 24, 32, 24)

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

        layout.addSpacing(4)

        self._group = QButtonGroup(self)

        options = [
            ("qt", "Full GUI (Recommended)",
             "Complete PyQt5 interface with all features, sidebar, persistent terminal, "
             "command palette, and custom widgets."),
            ("tk", "Lightweight GUI",
             "Tkinter-based interface with core features. Lower resource usage. "
             "Good for older hardware or when PyQt5 is unavailable."),
            ("tui", "Terminal UI",
             "Textual-based terminal interface. Runs in any terminal emulator. "
             "Ideal for SSH sessions, headless servers, and cyberdeck deployments."),
        ]

        for i, (key, label, desc) in enumerate(options):
            card = QFrame()
            card.setObjectName("launcher-card")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 10, 12, 10)
            card_layout.setSpacing(4)

            radio = QRadioButton(label)
            radio.setFont(QFont("Segoe UI", 11, QFont.Bold))
            radio.setProperty("ui_key", key)
            if i == 0:
                radio.setChecked(True)
            self._group.addButton(radio, i)
            card_layout.addWidget(radio)

            desc_label = QLabel(desc)
            desc_label.setFont(QFont("Segoe UI", 9))
            desc_label.setStyleSheet("color: #8b949e; padding-left: 24px;")
            desc_label.setWordWrap(True)
            card_layout.addWidget(desc_label)

            layout.addWidget(card)

        layout.addStretch()

        btn = QPushButton("Launch")
        btn.setFont(QFont("Segoe UI", 11, QFont.Bold))
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(40)
        btn.setStyleSheet(
            "QPushButton { background: #39ff14; color: #0d1117; border: none; "
            "border-radius: 6px; padding: 0 32px; font-weight: bold; }"
            "QPushButton:hover { background: #2dd912; }"
            "QPushButton:pressed { background: #24b00f; }"
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
        pass

    return result
