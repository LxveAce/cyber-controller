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

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.core.posture import POSTURE_OFFENSE, POSTURE_RECON  # canonical (re-exported); never drift
from src.ui.qt.theme import colors as C

# Transient-toast tints by level. Kept literal (matches the app's danger palette in operate_tab) so
# a theme without semantic success/warn/error names still renders; info falls back to muted text.
_TOAST_COLORS = {"success": "#3fb950", "warning": "#d29922", "error": "#f85149"}


class _Destination(QPushButton):
    """One sidebar destination: a checkable button carrying an optional count badge."""

    def __init__(self, key: str, label: str, icon_text: str = "") -> None:
        super().__init__()
        self.key = key
        self._label = label
        self._icon = icon_text
        self._count = 0
        self._mode = "sidebar"
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self._render("sidebar")

    def _style(self, align: str) -> str:
        return (
            f"QPushButton{{text-align:{align}; padding:6px 10px; border:none; color:{C.TEXT_MUTED};"
            f" background:transparent;}}"
            f"QPushButton:checked{{color:{C.TEXT_PRIMARY}; background:{C.BG_CARD};"
            f" border-left:2px solid {C.ACCENT};}}"
            f"QPushButton:hover{{color:{C.TEXT_PRIMARY};}}")

    def set_count(self, count: int) -> None:
        self._count = max(0, int(count))
        self._render(self._mode)

    def _render(self, mode: str) -> None:
        """Render for one nav mode: 'sidebar' = icon + label inline; 'rail' = icon OVER a tiny label
        (a legible ~64px touch cell for the deck — replaces the old icon-only 44px collapse)."""
        self._mode = mode
        badge = f"  ({self._count})" if self._count > 0 else ""
        icon = self._icon or "•"
        if mode == "rail":
            self.setText(f"{icon}\n{self._label}")   # icon over label
            self.setToolTip(f"{self._label}{badge}")
            self.setStyleSheet(self._style("center"))
        else:  # sidebar
            self.setText(f"{icon}  {self._label}{badge}")
            self.setToolTip("")
            self.setStyleSheet(self._style("left"))


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
        self._nav_mode = "sidebar"   # "sidebar" | "rail" | "bottombar" (Spade v2 nav-chrome)
        self._content: Optional[QWidget] = None
        self._toast_timer = QTimer(self)   # transient-toast auto-clear
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._clear_toast)

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
        # Spade v2: a bottom action bar that docks a surface's primary Start/Stop bottom-RIGHT (the
        # right-thumb arc) on the rail/bottombar deck. Hidden on the sidebar (the action is inline
        # there). set_content inserts ABOVE it; set_primary_action fills it.
        self._primary_action: "Optional[QWidget]" = None
        self._primary_bar = QWidget()
        _pb = QHBoxLayout(self._primary_bar)
        _pb.setContentsMargins(8, 4, 8, 8)
        _pb.addStretch(1)   # push the action to the right
        self._primary_bar.setVisible(False)
        self._content_holder.addWidget(self._primary_bar)
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
        # transient-toast slot (separate from the persistent device-truth slots above) — see toast()
        self._toast_label = QLabel("")
        self._toast_label.setVisible(False)
        h.addWidget(self._toast_label)
        h.addStretch(1)
        self._status_bar_layout = h   # host widgets (add_status_widget) insert before the omnibar
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
        if self._nav_mode != "sidebar":   # match the current nav mode if we're already on a rail
            dest._render("rail")
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

    def add_sidebar_widget(self, widget: QWidget) -> None:
        """Add an arbitrary widget to the sidebar below the destinations (e.g. a device/context
        panel folded into the one shell sidebar). Inserted above the trailing stretch."""
        self._sidebar_layout.insertWidget(self._sidebar_layout.count() - 1, widget)

    def set_badge(self, key: str, count: int) -> None:
        """Set a destination's live count badge (hidden at 0)."""
        dest = self._destinations.get(key)
        if dest is not None:
            dest.set_count(count)

    def set_content(self, widget: QWidget) -> None:
        """Set the central content widget (replaces any previous). Inserted ABOVE the primary-action
        bar so a docked Start/Stop stays pinned to the bottom."""
        if self._content is not None:
            self._content.setParent(None)
        self._content = widget
        self._content_holder.insertWidget(0, widget)

    def set_primary_action(self, widget: "Optional[QWidget]") -> None:
        """Dock a surface's primary action (its Start/Stop) bottom-RIGHT of the content — the
        right-thumb arc on the rail/bottombar deck. Shown only on rail/bottombar (on the sidebar
        the action lives inline in the surface). Pass None to clear."""
        if self._primary_action is not None:
            self._primary_action.setParent(None)
        self._primary_action = widget
        if widget is not None:
            self._primary_bar.layout().addWidget(widget)   # after the stretch -> right-aligned
        self._update_primary_bar()

    def _update_primary_bar(self) -> None:
        show = self._primary_action is not None and self._nav_mode != "sidebar"
        self._primary_bar.setVisible(show)

    def set_touch_density(self, min_target_pt: int) -> None:
        """Lift the shell's interactive controls to a minimum hit-target height (Spade v2 touch
        density): on a touch deck every button/input is >= min_target_pt tall. Reversible (a pointer
        profile clears it). Additive — min-height only; the theme's colours are untouched."""
        from src.ui.qt.layout_profile import min_target_qss
        self.setStyleSheet(min_target_qss(min_target_pt))

    def set_status(self, key: str, text: str, color: "Optional[str]" = None) -> None:
        """Set a top-bar device-truth field (link/battery/sd/gps/task/armed). Empty hides it."""
        lbl = self._status.get(key)
        if lbl is None:
            return
        lbl.setText(text)
        lbl.setVisible(bool(text))
        lbl.setStyleSheet(f"color:{color or C.TEXT_MUTED}; padding:0 6px;")

    def toast(self, message: str, level: str = "info", timeout: int = 4000) -> None:
        """Show a TRANSIENT status message in the one shell bar, auto-clearing after *timeout* ms.

        This is the single home for the fleeting "action X ran / failed" notices that used to
        scatter onto a second, bottom ``QMainWindow.statusBar()`` — distinct from the persistent
        device-truth slots (:meth:`set_status`) and count badges (:meth:`set_badge`). ``level`` in
        ``info``/``success``/``warning``/``error`` tints the text; ``timeout <= 0`` holds the
        message until the next toast or an explicit empty one."""
        self._toast_timer.stop()
        color = _TOAST_COLORS.get(level, C.TEXT_MUTED)
        self._toast_label.setText(message)
        self._toast_label.setStyleSheet(f"color:{color}; padding:0 8px; font-weight:600;")
        self._toast_label.setVisible(bool(message))
        if message and timeout and timeout > 0:
            self._toast_timer.start(int(timeout))

    def _clear_toast(self) -> None:
        self._toast_label.clear()
        self._toast_label.setVisible(False)

    def add_status_widget(self, widget: QWidget) -> None:
        """Add a host widget to the top status bar (right side, before the omnibar) — so chrome that
        used to live on a second bottom status bar folds into this one shell bar."""
        self._status_bar_layout.insertWidget(self._status_bar_layout.count() - 1, widget)

    def set_nav_mode(self, mode: str) -> None:
        """Render the surface nav three ways: the full labeled 'sidebar', a 64px icon-over-label
        'rail' (the 7" touch deck), or 'bottombar' (phone — interim: rail-rendered until the mobile
        bottom-bar lands). Idempotent, so a resize driver can call it every nav-mode change without
        fighting the user's manual toggle. Replaces the old icon-only 44px collapse."""
        if mode not in ("sidebar", "rail", "bottombar"):
            mode = "sidebar"
        if mode == self._nav_mode:
            return
        self._nav_mode = mode
        self._collapsed = mode != "sidebar"   # back-compat with the .collapsed property + callers
        if mode == "sidebar":
            self._sidebar.setMinimumWidth(160)
            self._sidebar.setMaximumWidth(220)
        else:  # rail / bottombar (interim): a fixed 64px icon-over-label rail
            self._sidebar.setMinimumWidth(64)
            self._sidebar.setMaximumWidth(64)
        dest_mode = "sidebar" if mode == "sidebar" else "rail"
        for d in self._destinations.values():
            d._render(dest_mode)
        self._update_primary_bar()   # the docked primary action shows only on rail/bottombar

    def set_collapsed(self, collapsed: bool) -> None:
        """Back-compat shim: collapse to the icon rail (True) or the full sidebar (False)."""
        self.set_nav_mode("rail" if collapsed else "sidebar")

    def toggle_sidebar(self) -> None:
        """Manual ≡ toggle between the full sidebar and the icon rail."""
        self.set_nav_mode("sidebar" if self._nav_mode != "sidebar" else "rail")

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

    @property
    def nav_mode(self) -> str:
        return self._nav_mode

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
