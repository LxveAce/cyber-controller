"""PageScreen — the shared master/detail/actions scaffold every screen mounts into.

The DESIGN-BRIEFS three-panel spine (Meshtastic Messages layout): a LEFT master (domain/scope), a
CENTER detail (the live content — table, detail view), and a RIGHT actions region (contextual
actions). A screen fills the three regions + declares its one primary action; it never touches the
shell sidebar/status bar directly (that is the PageLayout frame's job). The scaffold reflows the
three regions from side-by-side to a vertical stack via :meth:`relayout` off the shared
``page_screen_layout`` decider, so every screen inherits the SAME responsive structure instead of
ad-hoc inner chrome.

Display only — no send path, no safety surface. A screen's actions route through the F0 guarded-send
service, never through this scaffold.
"""
from __future__ import annotations

from typing import Any, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QSplitter, QVBoxLayout, QWidget


class PageScreen(QWidget):
    """A screen's master | detail | actions container. Mount into PageLayout via ``set_content``."""

    def __init__(self, nav_key: str, parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self.nav_key = nav_key
        self._primary_action: "Optional[QWidget]" = None
        self._last_size: "Optional[str]" = None   # last applied size class (for observability)

        # Each region is a holder with a single-child VBox, so set_* swaps content without touching
        # the splitter's child order (a holder is never removed from the splitter, only its inner
        # widget is replaced).
        self._master_holder, self._master_lay = self._region()
        self._detail_holder, self._detail_lay = self._region()
        self._actions_holder, self._actions_lay = self._region()

        self._splitter = QSplitter(Qt.Horizontal)
        self._splitter.addWidget(self._master_holder)
        self._splitter.addWidget(self._detail_holder)
        self._splitter.addWidget(self._actions_holder)
        self._splitter.setStretchFactor(0, 0)   # master: natural width
        self._splitter.setStretchFactor(1, 1)   # detail: takes the room
        self._splitter.setStretchFactor(2, 0)   # actions: natural width

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._splitter)

    @staticmethod
    def _region() -> "tuple[QWidget, QVBoxLayout]":
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        return holder, lay

    @staticmethod
    def _set_region(lay: QVBoxLayout, widget: "Optional[QWidget]") -> None:
        while lay.count():
            old = lay.takeAt(0).widget()
            if old is not None:
                old.setParent(None)
        if widget is not None:
            lay.addWidget(widget)

    # ── public API ────────────────────────────────────────────────────
    def set_master(self, widget: "Optional[QWidget]") -> None:
        """Set the LEFT master region (domain/scope selector, list). None clears it."""
        self._set_region(self._master_lay, widget)

    def set_detail(self, widget: "Optional[QWidget]") -> None:
        """Set the CENTER detail region (the screen's main content). None clears it."""
        self._set_region(self._detail_lay, widget)

    def set_actions(self, widget: "Optional[QWidget]") -> None:
        """Set the RIGHT actions region (contextual actions). None clears it."""
        self._set_region(self._actions_lay, widget)

    def set_primary_action(self, widget: "Optional[QWidget]") -> None:
        """Declare the screen's ONE primary action (its Start/Stop). The host docks it into the
        frame's action bar (``PageLayout.set_primary_action``); the scaffold only holds it."""
        self._primary_action = widget

    def primary_action(self) -> "Optional[QWidget]":
        """The declared primary action, for the host to hoist into the frame's docked action bar."""
        return self._primary_action

    def relayout(self, profile: Any) -> None:
        """Reflow the three regions for *profile* (a ``LayoutProfile``): side-by-side on
        regular/expanded, a vertical stack on compact; the actions region folds on the mid 'regular'
        width (its screen surfaces its primary action through the frame's docked bar there).
        Idempotent — cheap to call on every resize."""
        from src.ui.qt.layout_profile import page_screen_layout
        hl = page_screen_layout(profile)
        self._splitter.setOrientation(Qt.Vertical if hl.stack else Qt.Horizontal)
        self._actions_holder.setVisible(hl.show_actions)
        self._last_size = getattr(profile, "size", None)
