"""CP2 — Cardputer Remote: a Cardputer-shaped Device View + a raw CLI console.

Two honest input lanes, ONE guarded send path. In the app the `send` callback is
``main_window._device_view_send`` → firmware-match + safety-classifier confirmation + ``SerialConnection.write``'s
single-line / control-char validation. Both lanes here go through the *same* callback (wrapped once by
``_dispatch`` so every send — from either lane — lands in one honest transcript):

  (i)  **skin nav** — the reconstructed firmware menu, rendered at the Cardputer's real 240×135 (CP1's
       per-board ``native_size``). Activating a leaf dispatches that leaf's REAL firmware command; leaves whose
       command needs an argument the menu can't supply are ``needs_arg`` (DV4), so they never fire a broken
       bare line — that guard lives in the model and still holds through this composite.
  (ii) **raw console** — type a line, Enter sends it through the SAME ``_dispatch`` → same ``send``.

There is NO second/unguarded path to the device: the raw lane calls the identical dispatch the skin uses.
"""
from __future__ import annotations

from typing import Callable, Optional

from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.ui.qt.device_view import BOARD_SIZES, SKINS, DeviceScreenModel, DeviceView


class CardputerRemote(QWidget):
    """A Cardputer-shaped :class:`DeviceView` (skin-nav lane) above a raw CLI console lane, sharing one send."""

    def __init__(self, firmware: str, *, send: "Optional[Callable[[str], bool]]" = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._firmware = firmware
        self._raw_send = send   # the real guarded send (or None -> everything is a preview)

        # Lane (i): the reconstructed skin, shaped to the Cardputer's real 240×135 (CP1 native_size).
        title, factory = SKINS.get(firmware, SKINS["marauder"])
        skin_id = firmware if firmware in SKINS else "marauder"
        model = DeviceScreenModel(title, factory(), skin=skin_id, native_size=BOARD_SIZES["cardputer"])
        # Both lanes send through self._dispatch (embedding is safe: DeviceView._lock_aspect no-ops when the
        # view is not a top-level window, so it won't fight this layout — it just reports heightForWidth).
        self._view = DeviceView(model, send=self._dispatch)

        # Lane (ii): the raw console + a shared, bounded transcript of everything sent (either lane).
        self._console = QPlainTextEdit()
        self._console.setReadOnly(True)
        self._console.setMaximumBlockCount(500)          # bound memory — a long session can't grow unbounded
        self._console.setFont(QFont("JetBrains Mono", 9))
        self._console.setPlaceholderText("Commands sent to the device appear here (skin taps and raw lines).")

        self._input = QLineEdit()
        self._input.setPlaceholderText("raw command — Enter sends to the connected device")
        self._input.setClearButtonEnabled(True)
        self._input.returnPressed.connect(self._submit_raw)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)
        lay.addWidget(self._view, 1)
        lay.addWidget(QLabel("Raw console"))
        lay.addWidget(self._console, 0)
        lay.addWidget(self._input, 0)

    # ── the one guarded dispatch both lanes use ──────────────────────
    def _dispatch(self, cmd: str) -> bool:
        """Send *cmd* through the real guarded callback and record the honest outcome in the transcript.

        Returns True only if it actually went to a device — so the skin's own "sent"/"preview" status stays
        truthful (the DeviceView calls this as its ``send``), and the console shows the same truth.
        """
        sent = False
        if self._raw_send is not None:
            try:
                sent = bool(self._raw_send(cmd))
            except Exception:  # noqa: BLE001 — a send failure must never crash the console
                sent = False
        self._console.appendPlainText(("» sent: " if sent else "preview: ") + cmd)
        return sent

    # ── lane (ii) submit ─────────────────────────────────────────────
    def _submit_raw(self) -> None:
        text = self._input.text().strip()
        if not text:
            return                       # empty line -> nothing sent
        # NOTE: interior control chars are NOT stripped here — they are passed verbatim so the SAME
        # SerialConnection.write validation (via _dispatch -> send) rejects them, rather than a second
        # sanitizer silently diverging from the real guard.
        self._dispatch(text)
        self._input.clear()

    @property
    def view(self) -> DeviceView:
        return self._view
