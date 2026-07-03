"""Update-check dialogs — the two modal surfaces for the in-app updater.

Two independent dialogs, each with its OWN "Don't show again" checkbox so their suppression state
never bleeds together (the version prompt and the offline-error notice are gated by different
settings fields):

* :class:`UpdateAvailableDialog` — "vX is available". Update (opens the release page in the browser
  via QDesktopServices), Dismiss, and a "Don't show again" checkbox that (when ticked on Dismiss)
  tells the caller to suppress future version prompts.
* :class:`OfflineErrorDialog` — an info notice that the check couldn't reach GitHub, with its own
  "Don't show again" checkbox (suppresses only the offline notice; never the version prompt).

The dialogs contain NO settings/persistence logic — they just report the user's choice back to the
caller (the main window), which owns the settings writes.
"""

from __future__ import annotations

from PyQt5.QtCore import QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

# Action results for UpdateAvailableDialog.
ACTION_UPDATE = "update"
ACTION_DISMISS = "dismiss"


class UpdateAvailableDialog(QDialog):
    """Modal shown when a newer release exists. Update opens the release URL; Dismiss closes it."""

    def __init__(self, latest_tag: str, current_version: str, release_url: str,
                 behind: int = 1, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Update available")
        self._url = release_url or ""
        self._action = ACTION_DISMISS

        layout = QVBoxLayout(self)
        headline = QLabel(f"<b>{latest_tag} is available</b>")
        layout.addWidget(headline)
        more = (f"You're running v{current_version}."
                + (f" You are {behind} releases behind." if behind > 1 else ""))
        body = QLabel(more + "\n\nOpen the release page to download the new version?")
        body.setWordWrap(True)
        layout.addWidget(body)

        self._dont_show = QCheckBox("Don't show again")
        layout.addWidget(self._dont_show)

        buttons = QDialogButtonBox()
        update_btn = QPushButton("Update")
        update_btn.setDefault(True)
        dismiss_btn = QPushButton("Dismiss")
        buttons.addButton(update_btn, QDialogButtonBox.AcceptRole)
        buttons.addButton(dismiss_btn, QDialogButtonBox.RejectRole)
        update_btn.clicked.connect(self._on_update)
        dismiss_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def _on_update(self) -> None:
        if self._url:
            QDesktopServices.openUrl(QUrl(self._url))
        self._action = ACTION_UPDATE
        self.accept()

    def dont_show_again(self) -> bool:
        return self._dont_show.isChecked()

    def action(self) -> str:
        return self._action


class OfflineErrorDialog(QDialog):
    """Info notice: the update check couldn't reach GitHub. Has its own don't-show-again checkbox."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Update check unavailable")

        layout = QVBoxLayout(self)
        body = QLabel(
            "Couldn't reach GitHub to check for updates.\n\n"
            "This is usually a temporary network issue — the app works normally offline and will "
            "check again next time."
        )
        body.setWordWrap(True)
        layout.addWidget(body)

        self._dont_show = QCheckBox("Don't show this again")
        layout.addWidget(self._dont_show)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def dont_show_again(self) -> bool:
        return self._dont_show.isChecked()
