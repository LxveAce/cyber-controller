"""Shared PageLayout frame — the one shell every screen inherits (GUI rebuild, Wave-10 Phase B1).

The design brief's single biggest anti-template move: every view is structurally identical, only the
content differs. This frame provides that structure = a collapsible LEFT icon sidebar (destinations
carrying live count-badge slots) + a persistent TOP status bar (device-truth slots: link / battery /
SD / GPS / task / ARMED) + a header POSTURE toggle (Recon-Defense <-> gated Offense) + an OMNIBAR
slot (command input fused with fuzzy search) + a central content area.

This is the reusable frame COMPONENT only: additive, standalone, no main_window coupling. Wiring
the badges/status/posture to the hub is Phase B2/B3; re-parenting the app in is Phase C. Signals
let a host observe navigation, posture, and omnibar input without this frame knowing the app.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.ui.qt.theme import colors as C

POSTURE_RECON = "recon"      # default: passive recon / defence
POSTURE_OFFENSE = "offense"  # gated: active/offensive ops (the host logs + authorises the switch)


class _Destination(QPushButton):
    """One sidebar destination: a checkable button carrying an optional count badge."""

    def __init__(self, key: str, label: str, icon_text: str = "") -> None:
        super().__init__()
        self.key = key
        self._label = label
        self._icon = icon_text
        self._count = 0
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            f"QPushButton{{text-align:left; padding:6px 10px; border:none; color:{C.TEXT_MUTED};"
            f" background:transparent;}}"
            f"QPushButton:checked{{color:{C.TEXT_PRIMARY}; background:{C.BG_CARD};"
            f" border-left:2px solid {C.ACCENT};}}"
            f"QPushButton:hover{{color:{C.TEXT_PRIMARY};}}")
        self._render(collapsed=False)

    def set_count(self, count: int) -> None:
        self._count = max(0, int(count))
        self._render(self._collapsed)

    def _render(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        badge = f"  ({self._count})" if self._count > 0 else ""
        icon = self._icon or "•"
        self.setText(icon if collapsed else f"{icon}  {self._label}{badge}")
        self.setToolTip(f"{self._label}{badge}" if collapsed else "")


class PageLayout(QWidget):
    """The shared shell frame: sidebar + status bar + posture toggle + omnibar + content."""

    destination_selected = pyqtSignal(str)  # a sidebar destination key was chosen
    posture_changed = pyqtSignal(str)       # posture ACTUALLY changed (after any auth) -> new value
    # Escalating to Offense is a BOUNDARY: the click emits this REQUEST and does NOT flip. The host
    # runs the authorization confirm/log, then calls set_posture(POSTURE_OFFENSE) to actually apply.
    posture_escalation_requested = pyqtSignal(str)
    omnibar_submitted = pyqtSignal(str)     # the operator entered a command / search

    def __init__(self, parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._destinations: dict[str, _Destination] = {}
        self._status: dict[str, QLabel] = {}
        self._posture = POSTURE_RECON
        self._collapsed = False
        self._content: Optional[QWidget] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_status_bar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())
        self._content_holder = QVBoxLayout()
        self._content_holder.setContentsMargins(0, 0, 0, 0)
        holder = QWidget()
        holder.setLayout(self._content_holder)
        body.addWidget(holder, 1)
        outer.addLayout(body, 1)

    # ── status bar (top) ─────────────────────────────────────────────
    def _build_status_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("page_status_bar")
        bar.setStyleSheet(
            f"#page_status_bar{{background:{C.BG_SURFACE}; border-bottom:1px solid {C.BORDER};}}")
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 4, 8, 4)
        # collapse toggle for the sidebar
        self._collapse_btn = QPushButton("≡")  # ≡
        self._collapse_btn.setFixedWidth(28)
        self._collapse_btn.setCursor(Qt.PointingHandCursor)
        self._collapse_btn.clicked.connect(self.toggle_sidebar)
        h.addWidget(self._collapse_btn)
        # posture toggle
        self._posture_btn = QPushButton()
        self._posture_btn.setCheckable(True)
        self._posture_btn.setCursor(Qt.PointingHandCursor)
        self._posture_btn.clicked.connect(self._on_posture_clicked)
        self._render_posture()
        h.addWidget(self._posture_btn)
        # device-truth status fields (slots; wired in B2)
        for key in ("link", "battery", "sd", "gps", "task", "armed"):
            lbl = QLabel("")
            lbl.setStyleSheet(f"color:{C.TEXT_MUTED}; padding:0 6px;")
            lbl.setVisible(False)
            self._status[key] = lbl
            h.addWidget(lbl)
        h.addStretch(1)
        # omnibar (command + fuzzy search fused)
        self._omnibar = QLineEdit()
        self._omnibar.setPlaceholderText("Command or search…")
        self._omnibar.setFixedWidth(240)
        self._omnibar.setStyleSheet(
            f"background:{C.BG_INPUT}; color:{C.TEXT_PRIMARY}; border:1px solid {C.BORDER};"
            f" border-radius:4px; padding:3px 6px;")
        self._omnibar.returnPressed.connect(self._on_omnibar)
        h.addWidget(self._omnibar)
        return bar

    # ── sidebar (left) ───────────────────────────────────────────────
    def _build_sidebar(self) -> QWidget:
        self._sidebar = QFrame()
        self._sidebar.setObjectName("page_sidebar")
        self._sidebar.setStyleSheet(
            f"#page_sidebar{{background:{C.BG_SURFACE}; border-right:1px solid {C.BORDER};}}")
        self._sidebar.setMinimumWidth(160)
        self._sidebar.setMaximumWidth(220)
        self._sidebar_layout = QVBoxLayout(self._sidebar)
        self._sidebar_layout.setContentsMargins(0, 4, 0, 4)
        self._sidebar_layout.setSpacing(1)
        self._sidebar_layout.addStretch(1)
        return self._sidebar

    # ── public API ───────────────────────────────────────────────────
    def add_destination(self, key: str, label: str, icon_text: str = "") -> None:
        """Add a sidebar destination. Selecting it emits ``destination_selected(key)``."""
        if key in self._destinations:
            return
        dest = _Destination(key, label, icon_text)
        dest.clicked.connect(lambda _checked=False, k=key: self.select_destination(k))
        self._destinations[key] = dest
        # insert above the trailing stretch
        self._sidebar_layout.insertWidget(self._sidebar_layout.count() - 1, dest)

    def select_destination(self, key: str) -> None:
        """Select *key* (checks its button, unchecks the rest) and emit the signal."""
        if key not in self._destinations:
            return
        for k, d in self._destinations.items():
            d.setChecked(k == key)
        self.destination_selected.emit(key)

    def highlight_destination(self, key: str) -> None:
        """Check *key* + uncheck the rest WITHOUT emitting — for reflecting an external nav change
        (e.g. the host switched surface via another control) with no re-navigation feedback loop."""
        if key not in self._destinations:
            return
        for k, d in self._destinations.items():
            d.setChecked(k == key)

    def set_destination_visible(self, key: str, visible: bool) -> None:
        """Show/hide a destination — so a nav rail can mirror which surfaces are available."""
        dest = self._destinations.get(key)
        if dest is not None:
            dest.setVisible(visible)

    def set_badge(self, key: str, count: int) -> None:
        """Set a destination's live count badge (hidden at 0)."""
        dest = self._destinations.get(key)
        if dest is not None:
            dest.set_count(count)

    def set_content(self, widget: QWidget) -> None:
        """Set the central content widget (replaces any previous)."""
        if self._content is not None:
            self._content.setParent(None)
        self._content = widget
        self._content_holder.addWidget(widget)

    def set_status(self, key: str, text: str, color: "Optional[str]" = None) -> None:
        """Set a top-bar device-truth field (link/battery/sd/gps/task/armed). Empty hides it."""
        lbl = self._status.get(key)
        if lbl is None:
            return
        lbl.setText(text)
        lbl.setVisible(bool(text))
        lbl.setStyleSheet(f"color:{color or C.TEXT_MUTED}; padding:0 6px;")

    def toggle_sidebar(self) -> None:
        """Collapse/expand the sidebar to an icon rail."""
        self._collapsed = not self._collapsed
        self._sidebar.setMaximumWidth(44 if self._collapsed else 220)
        self._sidebar.setMinimumWidth(44 if self._collapsed else 160)
        for d in self._destinations.values():
            d._render(self._collapsed)

    @property
    def posture(self) -> str:
        return self._posture

    def set_posture(self, posture: str) -> None:
        """Set the global posture programmatically (host use, e.g. after an auth confirm)."""
        if posture not in (POSTURE_RECON, POSTURE_OFFENSE) or posture == self._posture:
            return
        self._posture = posture
        self._render_posture()
        self.posture_changed.emit(posture)

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    # ── internals ────────────────────────────────────────────────────
    def _on_posture_clicked(self) -> None:
        if self._posture == POSTURE_OFFENSE:
            # De-escalating to Recon (dropping to passive) is always safe -> apply immediately.
            self.set_posture(POSTURE_RECON)
            return
        # Escalating Recon -> Offense is a BOUNDARY: emit a REQUEST and DO NOT flip. The host runs
        # the auth confirm/log, then calls set_posture(POSTURE_OFFENSE). Revert the checkable button
        # to the still-current Recon state so one click can never silently arm Offense.
        self._render_posture()
        self.posture_escalation_requested.emit(POSTURE_OFFENSE)

    def _render_posture(self) -> None:
        offense = self._posture == POSTURE_OFFENSE
        self._posture_btn.setChecked(offense)
        self._posture_btn.setText("Offense" if offense else "Recon / Defense")
        col = C.ALERT if offense else C.SUCCESS
        self._posture_btn.setStyleSheet(
            f"QPushButton{{color:{col}; border:1px solid {col}; border-radius:4px;"
            f" padding:2px 8px;}}")

    def _on_omnibar(self) -> None:
        text = self._omnibar.text().strip()
        if text:
            self.omnibar_submitted.emit(text)
