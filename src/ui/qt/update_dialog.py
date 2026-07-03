"""Update-check dialogs — the two modal surfaces for the in-app updater.

Two independent dialogs, each with its OWN "Don't show again" checkbox so their suppression state
never bleeds together (the version prompt and the offline-error notice are gated by different
settings fields):

* :class:`UpdateAvailableDialog` — "vX is available". On a frozen build it offers **Download &
  install** (an in-place self-update the caller runs, which downloads + verifies + swaps the binary
  and restarts) with **Open release page** as a fallback; on a source build it only offers the
  browser deep-link. Plus Dismiss and a "Don't show again" checkbox that (when ticked on Dismiss)
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
ACTION_UPDATE = "update"            # user chose the browser deep-link to the release page
ACTION_SELF_UPDATE = "self_update"  # user chose the in-place download-and-install
ACTION_DISMISS = "dismiss"


class UpdateAvailableDialog(QDialog):
    """Modal shown when a newer release exists.

    When *can_self_update* is True (a frozen build) the primary action is **Download & install** —
    the caller runs the in-place self-update and the app restarts — with **Open release page** as a
    fallback. On a source build only the deep-link is offered. Dismiss closes it.
    """

    def __init__(self, latest_tag: str, current_version: str, release_url: str,
                 behind: int = 1, parent=None, can_self_update: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Update available")
        self._url = release_url or ""
        self._action = ACTION_DISMISS

        layout = QVBoxLayout(self)
        headline = QLabel(f"<b>{latest_tag} is available</b>")
        layout.addWidget(headline)
        more = (f"You're running v{current_version}."
                + (f" You are {behind} releases behind." if behind > 1 else ""))
        prompt = ("\n\nDownload and install it now? The app will verify the download and restart."
                  if can_self_update
                  else "\n\nOpen the release page to download the new version?")
        body = QLabel(more + prompt)
        body.setWordWrap(True)
        layout.addWidget(body)

        self._dont_show = QCheckBox("Don't show again")
        layout.addWidget(self._dont_show)

        buttons = QDialogButtonBox()
        if can_self_update:
            install_btn = QPushButton("Download && install")  # && → one literal ampersand in Qt
            install_btn.setDefault(True)
            buttons.addButton(install_btn, QDialogButtonBox.AcceptRole)
            install_btn.clicked.connect(self._on_self_update)
            page_btn = QPushButton("Open release page")
            buttons.addButton(page_btn, QDialogButtonBox.ActionRole)
            page_btn.clicked.connect(self._on_update)
        else:
            update_btn = QPushButton("Update")
            update_btn.setDefault(True)
            buttons.addButton(update_btn, QDialogButtonBox.AcceptRole)
            update_btn.clicked.connect(self._on_update)
        dismiss_btn = QPushButton("Dismiss")
        buttons.addButton(dismiss_btn, QDialogButtonBox.RejectRole)
        dismiss_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def _on_update(self) -> None:
        if self._url:
            QDesktopServices.openUrl(QUrl(self._url))
        self._action = ACTION_UPDATE
        self.accept()

    def _on_self_update(self) -> None:
        self._action = ACTION_SELF_UPDATE
        self.accept()

    def dont_show_again(self) -> bool:
        return self._dont_show.isChecked()

    def action(self) -> str:
        return self._action


class OfflineErrorDialog(QDialog):
    """Info notice: the update check couldn't reach GitHub. Has a don't-show-again checkbox."""

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
