"""How-To tab — renders the bundled docs/HOWTO.md so every feature is explained in-app (offline)."""

from __future__ import annotations

import logging

from PyQt5.QtWidgets import QTextBrowser, QVBoxLayout, QWidget

from src.core.resources import resource_path

log = logging.getLogger(__name__)


class HowToTab(QWidget):
    """In-app usage guide (renders docs/HOWTO.md)."""

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._view = QTextBrowser()
        self._view.setOpenExternalLinks(True)
        layout.addWidget(self._view)
        self._load()

    def _load(self) -> None:
        path = resource_path("docs", "HOWTO.md")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            self._view.setPlainText(f"How-To guide unavailable: {exc}")
            return
        # QTextBrowser.setMarkdown is available on Qt 5.14+; fall back to plain text otherwise.
        if hasattr(self._view, "setMarkdown"):
            self._view.setMarkdown(text)
        else:
            self._view.setPlainText(text)
