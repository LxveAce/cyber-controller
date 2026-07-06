"""Bug-report dialog — compose a note, preview the redacted diagnostics bundle, and save/copy/submit it.

Opened from Help ▸ Report a Bug. Everything shown is auto-redacted (see src.core.diagnostics), so the
user can safely save it to a file, copy it, or open a prefilled GitHub issue to "send it back for fixing".
"""

from __future__ import annotations

import logging
import webbrowser
from pathlib import Path

from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from src.core.diagnostics import collect_report, github_issue_url

log = logging.getLogger(__name__)


class BugReportDialog(QDialog):
    """Collect a user description + redacted diagnostics and export it."""

    def __init__(self, parent=None, extra: dict | None = None) -> None:
        super().__init__(parent)
        self._extra = extra or {}
        self.setWindowTitle("Report a Bug")
        self.resize(660, 580)

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Describe what went wrong (what you did, what you expected, what happened):"))

        self._note = QPlainTextEdit()
        self._note.setPlaceholderText(
            "e.g. Flashing COM5 with Marauder failed at 'Connecting…' — board is a CYD 2.8\"."
        )
        self._note.setMaximumHeight(120)
        self._note.textChanged.connect(self._refresh_preview)
        root.addWidget(self._note)

        root.addWidget(QLabel(
            "Included diagnostics (auto-redacted — no tokens, emails, home path, or your username):"
        ))
        self._preview = QTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setObjectName("terminal")
        root.addWidget(self._preview, stretch=1)

        row = QHBoxLayout()
        btn_save = QPushButton("Save to File…")
        btn_save.setToolTip("Write the report to a .txt file you can send to the maintainer.")
        btn_save.clicked.connect(self._save)
        btn_copy = QPushButton("Copy to Clipboard")
        btn_copy.clicked.connect(self._copy)
        btn_gh = QPushButton("Open GitHub Issue")
        btn_gh.setToolTip("Open a prefilled new-issue page in your browser.")
        btn_gh.clicked.connect(self._github)
        for b in (btn_save, btn_copy, btn_gh):
            row.addWidget(b)
        row.addStretch(1)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.reject)
        row.addWidget(btn_close)
        root.addLayout(row)

        self._refresh_preview()

    # ── internals ────────────────────────────────────────────────────

    def _report_text(self) -> str:
        return collect_report(self._note.toPlainText(), extra=self._extra)

    def _refresh_preview(self) -> None:
        self._preview.setPlainText(self._report_text())

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save bug report", "cyber-controller-bugreport.txt", "Text (*.txt)"
        )
        if not path:
            return
        try:
            Path(path).write_text(self._report_text(), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Save failed", str(exc))
            return
        QMessageBox.information(self, "Saved", f"Bug report saved to:\n{path}")

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._report_text())
        QMessageBox.information(
            self, "Copied", "Bug report copied to the clipboard — paste it wherever you send it."
        )

    def _github(self) -> None:
        note = self._note.toPlainText().strip()
        title = f"Bug: {note.splitlines()[0][:80]}" if note else "Bug report"
        try:
            webbrowser.open(github_issue_url(title, self._report_text()))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Could not open browser", str(exc))
