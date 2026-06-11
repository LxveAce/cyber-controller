"""Command palette — VS Code-style Ctrl+Shift+P fuzzy command launcher."""

from __future__ import annotations

from typing import Callable

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)


class _Command:
    """Internal command descriptor."""

    __slots__ = ("label", "callback")

    def __init__(self, label: str, callback: Callable[[], None]) -> None:
        self.label = label
        self.callback = callback


def _fuzzy_match(query: str, text: str) -> bool:
    """Return True if every character in *query* appears in *text* in order (case-insensitive)."""
    qi = 0
    query_lower = query.lower()
    text_lower = text.lower()
    for ch in text_lower:
        if qi < len(query_lower) and ch == query_lower[qi]:
            qi += 1
    return qi == len(query_lower)


class CommandPalette(QDialog):
    """A searchable command palette dialog.

    Registers a flat list of (label, callback) commands. The user types to fuzzy-filter, then
    presses Enter to execute the selected command and close.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Command Palette")
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setMinimumWidth(520)
        self.setMaximumWidth(620)
        self.setMinimumHeight(380)

        self._commands: list[_Command] = []
        self._build_ui()

    # ── UI ───────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.setStyleSheet(
            """
            CommandPalette {
                background-color: #161b22;
                border: 2px solid #39ff14;
                border-radius: 10px;
            }
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # Header
        header = QHBoxLayout()
        icon_label = QLabel(">")
        icon_label.setFont(QFont("JetBrains Mono", 14, QFont.Bold))
        icon_label.setStyleSheet("color: #39ff14; background: transparent;")
        header.addWidget(icon_label)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Type a command...")
        self._search.setFont(QFont("Segoe UI", 11))
        self._search.setStyleSheet(
            """
            QLineEdit {
                background-color: #0d1117;
                color: #e6edf3;
                border: 1px solid #30363d;
                border-radius: 6px;
                padding: 8px 12px;
                font-size: 11pt;
                selection-background-color: #39ff14;
                selection-color: #000;
            }
            QLineEdit:focus {
                border-color: #39ff14;
            }
            """
        )
        self._search.textChanged.connect(self._on_filter)
        self._search.returnPressed.connect(self._on_execute)
        header.addWidget(self._search)
        root.addLayout(header)

        # Command list
        self._list = QListWidget()
        self._list.setStyleSheet(
            """
            QListWidget {
                background-color: #0d1117;
                color: #e6edf3;
                border: 1px solid #30363d;
                border-radius: 6px;
                outline: none;
                padding: 4px;
                font-size: 10pt;
            }
            QListWidget::item {
                padding: 8px 12px;
                border-radius: 4px;
            }
            QListWidget::item:selected {
                background-color: #1c2128;
                color: #39ff14;
            }
            QListWidget::item:hover:!selected {
                background-color: #1c2128;
            }
            """
        )
        self._list.itemDoubleClicked.connect(lambda _: self._on_execute())
        root.addWidget(self._list)

        # Hint
        hint = QLabel("Enter to run  |  Esc to close")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet("color: #484f58; font-size: 8pt; background: transparent;")
        root.addWidget(hint)

    # ── Public API ──────────────────────────────────────────────────

    def add_command(self, label: str, callback: Callable[[], None]) -> None:
        """Register a command entry."""
        self._commands.append(_Command(label, callback))

    def open_palette(self) -> None:
        """Show the palette, reset filter, and focus the search box."""
        self._search.clear()
        self._populate_list(self._commands)
        self._search.setFocus()
        self.exec_()

    # ── Internal ────────────────────────────────────────────────────

    def _populate_list(self, commands: list[_Command]) -> None:
        self._list.clear()
        for cmd in commands:
            item = QListWidgetItem(cmd.label)
            item.setData(Qt.UserRole, cmd)
            self._list.addItem(item)
        if self._list.count() > 0:
            self._list.setCurrentRow(0)

    def _on_filter(self, text: str) -> None:
        query = text.strip()
        if not query:
            self._populate_list(self._commands)
            return
        filtered = [c for c in self._commands if _fuzzy_match(query, c.label)]
        self._populate_list(filtered)

    def _on_execute(self) -> None:
        current = self._list.currentItem()
        if current is None:
            return
        cmd: _Command = current.data(Qt.UserRole)
        if cmd and cmd.callback:
            self.accept()
            cmd.callback()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key_Escape:
            self.reject()
        elif key == Qt.Key_Down:
            row = self._list.currentRow()
            if row < self._list.count() - 1:
                self._list.setCurrentRow(row + 1)
        elif key == Qt.Key_Up:
            row = self._list.currentRow()
            if row > 0:
                self._list.setCurrentRow(row - 1)
        else:
            super().keyPressEvent(event)
