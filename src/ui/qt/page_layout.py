"""Shared PageLayout frame — the one shell every screen inherits (GUI rebuild, Wave-10 Phase B1).

The design brief's single biggest anti-template move: every view is structurally identical, only the
content differs. This frame provides that structure = a LEFT nav rail (verb destinations at top,
Settings pinned bottom, each carrying a live count-badge slot) + a persistent TOP bar (brand ·
breadcrumb · live device-truth · a prominent SAFE/ARMED lamp · a Simple/Pro depth segment · pop-out
and settings icons) + a central content area.

Reform pass (2026-08-06): the rendering now matches the owner-approved reform mockup — brand mark +
title, a self-updating breadcrumb, the ``● SAFE`` state lamp, the Pro/Simple segmented control, and
``⤢``/``⚙`` icon buttons — while EVERY public method and signal the app (main_window) and the
PageLayoutBinder call is preserved unchanged, so the shell wiring keeps working. Signals let a host
observe navigation, posture, depth and omnibar input without this frame knowing the app.
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

# Mockup chrome literals (GitHub-Primer): the rail/topbar sit on the deepest surface, a step below
# the canvas, so the content reads as raised. Kept literal to match the approved mockup exactly.
_RAIL_BG = "#010409"
_RAIL_BD = "#21262d"
_TX2 = "#f0f6fc"

# Rail glyphs by destination key — the mockup's per-verb marks. main_window adds destinations by key
# without an icon, so the frame supplies the glyph itself (a leading substring match keeps it robust
# to exact labels: "software-os" etc. never collide with a verb key).
_NAV_GLYPHS = {
    "device": "◫", "rig": "◫", "hunt": "◎", "operate": "⌘", "crack": "⚷",
    "map": "⌖", "terminal": "❯", "settings": "⚙",
}
# Destinations pinned to the BOTTOM of the rail (below the grow spacer), like the mockup.
_BOTTOM_KEYS = {"terminal", "settings"}


def _glyph_for(key: str, fallback: str = "•") -> str:
    k = (key or "").lower()
    if k in _NAV_GLYPHS:
        return _NAV_GLYPHS[k]
    for name, g in _NAV_GLYPHS.items():
        if k.startswith(name):
            return g
    return fallback


class _Destination(QPushButton):
    """One nav-rail destination: a checkable button (glyph + label + optional count badge)."""

    def __init__(self, key: str, label: str, icon_text: str = "") -> None:
        super().__init__()
        self.key = key
        self._label = label
        self._icon = icon_text or _glyph_for(key)
        self._count = 0
        self._mode = "sidebar"
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self._render("sidebar")

    def _style(self, align: str) -> str:
        # .navitem: rounded row, 3px left rule that lights to the accent when active; the active row
        # sits on the card surface so it reads as "you are here".
        return (
            f"QPushButton{{text-align:{align}; padding:9px 10px; border:none; border-radius:8px;"
            f" border-left:3px solid transparent; color:{C.TEXT_MUTED}; background:transparent;"
            f" font-weight:600; letter-spacing:0.3px; font-size:12px;}}"
            f"QPushButton:hover{{color:{_TX2}; background:{C.BG_DEEP};}}"
            f"QPushButton:checked{{color:{_TX2}; background:{C.BG_SURFACE};"
            f" border-left:3px solid {C.ACCENT};}}")

    def set_count(self, count: int) -> None:
        self._count = max(0, int(count))
        self._render(self._mode)

    def _render(self, mode: str) -> None:
        """Render for one nav mode: 'sidebar' = glyph + label inline; 'rail' = glyph OVER a tiny label
        (a legible ~64px touch cell for the deck)."""
        self._mode = mode
        badge = f"  ({self._count})" if self._count > 0 else ""
        icon = self._icon or "•"
        if mode == "rail":
            self.setText(f"{icon}\n{self._label}")   # icon over label
            self.setToolTip(f"{self._label}{badge}")
            self.setStyleSheet(self._style("center"))
        else:  # sidebar
            self.setText(f"{icon}   {self._label}{badge}")
            self.setToolTip("")
            self.setStyleSheet(self._style("left"))


class PageLayout(QWidget):
    """The shared shell frame: nav rail + top bar (brand/crumb/lamp/depth/icons) + content."""

    destination_selected = pyqtSignal(str)  # a nav destination key was chosen
    posture_changed = pyqtSignal(str)       # display posture changed (host-driven) -> new value
    omnibar_submitted = pyqtSignal(str)     # the operator entered a command / search
    depth_changed = pyqtSignal(str)         # Simple/Pro segmented control -> "simple" | "pro"
    detach_requested = pyqtSignal()         # the ⤢ pop-out icon was clicked

    def __init__(self, parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._destinations: dict[str, _Destination] = {}
        self._status: dict[str, QLabel] = {}
        self._posture = POSTURE_RECON
        self._depth = "pro"
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

    # ── top bar ──────────────────────────────────────────────────────
    def _build_status_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("page_status_bar")
        bar.setFixedHeight(44)
        bar.setStyleSheet(
            f"#page_status_bar{{background:{_RAIL_BG}; border-bottom:1px solid {_RAIL_BD};}}")
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 0, 12, 0)
        h.setSpacing(12)

        # Manual sidebar-collapse control kept for the API/tests + toggle_sidebar(), but hidden: the
        # responsive driver (main_window) folds the rail automatically, so the mockup shows no toggle.
        self._collapse_btn = QPushButton("≡")
        self._collapse_btn.setFixedWidth(28)
        self._collapse_btn.setCursor(Qt.PointingHandCursor)
        self._collapse_btn.clicked.connect(self.toggle_sidebar)
        self._collapse_btn.setVisible(False)
        h.addWidget(self._collapse_btn)

        # brand: gradient ◈ mark + product name
        mark = QLabel("◈")
        mark.setFixedSize(22, 22)
        mark.setAlignment(Qt.AlignCenter)
        mark.setStyleSheet(
            "QLabel{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,"
            f"stop:0 {C.ACCENT}, stop:1 {C.ACCENT_DIM}); color:#fff; border-radius:6px; font-size:12px;}}")
        h.addWidget(mark)
        brand = QLabel("Cyber Controller")
        brand.setStyleSheet(f"color:{_TX2}; font-weight:650; letter-spacing:0.2px;")
        h.addWidget(brand)

        # breadcrumb (self-updates on navigation; a host may add the leaf via set_breadcrumb)
        self._crumb = QLabel()
        self._crumb.setTextFormat(Qt.RichText)
        self._crumb.setStyleSheet(f"color:{C.TEXT_MUTED}; font-size:12px;")
        self.set_breadcrumb("DEVICE", "Dashboard")
        h.addWidget(self._crumb)

        h.addStretch(1)

        # posture indicator (display-only, gates nothing) — kept for API/binder but hidden: the reform
        # top bar shows the SAFE/ARMED lamp instead. set_posture still tracks + emits for the binder.
        self._posture_lbl = QLabel()
        self._render_posture()
        self._posture_lbl.setVisible(False)
        h.addWidget(self._posture_lbl)

        # device-truth status fields (slots; wired by the binder). 'armed' drives the lamp, so its own
        # label is parented+hidden (its text is still kept in sync as truth), while 'link' etc. render
        # inline, muted, before the lamp.
        for key in ("link", "battery", "sd", "gps", "task", "armed"):
            lbl = QLabel("")
            lbl.setStyleSheet(f"color:{C.TEXT_MUTED}; padding:0 6px; font-size:11.5px;")
            lbl.setVisible(False)
            self._status[key] = lbl
            if key == "armed":
                lbl.setParent(bar)   # kept as truth (tests read its text) but never shown; lamp is visual
            else:
                h.addWidget(lbl)

        # transient-toast slot (separate from the persistent device-truth slots above) — see toast()
        self._toast_label = QLabel("")
        self._toast_label.setVisible(False)
        h.addWidget(self._toast_label)

        # the SAFE / ARMED state lamp (the mockup's prominent pill)
        self._safe_lamp = QLabel()
        self._render_safe_lamp("")
        h.addWidget(self._safe_lamp)

        # Simple / Pro depth segmented control
        h.addWidget(self._build_depth_segment())

        # pop-out (detach) + settings icon buttons
        self._btn_detach = self._icon_button("⤢", "Pop out the current view into its own window")
        self._btn_detach.clicked.connect(self.detach_requested.emit)
        h.addWidget(self._btn_detach)
        self._btn_settings = self._icon_button("⚙", "Settings")
        self._btn_settings.clicked.connect(lambda: self.select_destination("settings"))
        h.addWidget(self._btn_settings)

        self._status_bar_layout = h   # host widgets (add_status_widget) insert before the icons

        # omnibar (command + fuzzy search) — kept + wired (returnPressed) for the API, but hidden to
        # match the mockup's clean bar; the command palette stays reachable via Ctrl+Shift+P.
        self._omnibar = QLineEdit()
        self._omnibar.setPlaceholderText("Command or search…")
        self._omnibar.setFixedWidth(240)
        self._omnibar.returnPressed.connect(self._on_omnibar)
        self._omnibar.setVisible(False)
        h.addWidget(self._omnibar)
        return bar

    def _icon_button(self, glyph: str, tip: str) -> QPushButton:
        btn = QPushButton(glyph)
        btn.setFixedSize(28, 28)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip(tip)
        btn.setStyleSheet(
            f"QPushButton{{border:1px solid {C.BORDER}; border-radius:7px; background:transparent;"
            f" color:{C.TEXT_MUTED}; font-size:14px;}}"
            f"QPushButton:hover{{color:{_TX2}; border-color:{C.TEXT_DIM};}}")
        return btn

    def _build_depth_segment(self) -> QWidget:
        seg = QFrame()
        seg.setObjectName("depth_seg")
        seg.setStyleSheet(f"#depth_seg{{border:1px solid {C.BORDER}; border-radius:7px;}}")
        row = QHBoxLayout(seg)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self._depth_btns: dict[str, QPushButton] = {}
        for mode, text in (("pro", "Pro"), ("simple", "Simple")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c=False, m=mode: self._on_depth_clicked(m))
            self._depth_btns[mode] = b
            row.addWidget(b)
        self._render_depth()
        return seg

    def _render_depth(self) -> None:
        for mode, b in self._depth_btns.items():
            on = mode == self._depth
            b.setChecked(on)
            if on:
                b.setStyleSheet(f"QPushButton{{background:{C.ACCENT}; color:#fff; border:0;"
                                f" padding:4px 11px; font-weight:600; font-size:11.5px;}}")
            else:
                b.setStyleSheet(f"QPushButton{{background:transparent; color:{C.TEXT_MUTED}; border:0;"
                                f" padding:4px 11px; font-size:11.5px;}}")

    def _on_depth_clicked(self, mode: str) -> None:
        if mode == self._depth:
            return
        self._depth = mode
        self._render_depth()
        self.depth_changed.emit(mode)

    def set_depth(self, mode: str) -> None:
        """Reflect the current Simple/Pro depth in the segment WITHOUT emitting (host-driven sync, e.g.
        the View menu changed the mode)."""
        mode = "simple" if str(mode).lower().startswith("s") else "pro"
        if mode == self._depth:
            return
        self._depth = mode
        self._render_depth()

    # ── nav rail (left) ──────────────────────────────────────────────
    def _build_sidebar(self) -> QWidget:
        self._sidebar = QFrame()
        self._sidebar.setObjectName("page_sidebar")
        self._sidebar.setStyleSheet(
            f"#page_sidebar{{background:{_RAIL_BG}; border-right:1px solid {_RAIL_BD};}}")
        self._sidebar.setMinimumWidth(160)
        self._sidebar.setMaximumWidth(220)
        self._sidebar_layout = QVBoxLayout(self._sidebar)
        self._sidebar_layout.setContentsMargins(8, 8, 8, 6)
        self._sidebar_layout.setSpacing(2)
        # top verbs, a grow spacer, then bottom-pinned destinations (Terminal/Settings) + a hint.
        self._nav_top = QVBoxLayout()
        self._nav_top.setContentsMargins(0, 0, 0, 0)
        self._nav_top.setSpacing(2)
        self._nav_bottom = QVBoxLayout()
        self._nav_bottom.setContentsMargins(0, 0, 0, 0)
        self._nav_bottom.setSpacing(2)
        self._sidebar_layout.addLayout(self._nav_top)
        self._sidebar_layout.addStretch(1)
        self._sidebar_layout.addLayout(self._nav_bottom)
        return self._sidebar

    # ── public API ───────────────────────────────────────────────────
    def add_destination(self, key: str, label: str, icon_text: str = "") -> None:
        """Add a nav destination. Selecting it emits ``destination_selected(key)``. Terminal/Settings
        pin to the bottom of the rail (below the grow spacer); every other verb sits at the top."""
        if key in self._destinations:
            return
        dest = _Destination(key, label, icon_text)
        if self._nav_mode != "sidebar":   # match the current nav mode if we're already on a rail
            dest._render("rail")
        dest.clicked.connect(lambda _checked=False, k=key: self.select_destination(k))
        self._destinations[key] = dest
        target = self._nav_bottom if key.lower() in _BOTTOM_KEYS else self._nav_top
        target.addWidget(dest)

    def select_destination(self, key: str) -> None:
        """Select *key* (checks its button, unchecks the rest) and emit the signal."""
        if key not in self._destinations:
            return
        for k, d in self._destinations.items():
            d.setChecked(k == key)
        self._sync_crumb(key)
        self.destination_selected.emit(key)

    def highlight_destination(self, key: str) -> None:
        """Check *key* + uncheck the rest WITHOUT emitting — for reflecting an external nav change
        (e.g. the host switched surface via another control) with no re-navigation feedback loop."""
        if key not in self._destinations:
            return
        for k, d in self._destinations.items():
            d.setChecked(k == key)
        self._sync_crumb(key)

    def set_destination_visible(self, key: str, visible: bool) -> None:
        """Show/hide a destination — so a nav rail can mirror which surfaces are available."""
        dest = self._destinations.get(key)
        if dest is not None:
            dest.setVisible(visible)

    def add_sidebar_widget(self, widget: QWidget) -> None:
        """Add an arbitrary widget to the rail, above the bottom-pinned destinations. (The reform rail
        is nav-only; the app no longer folds the device panel in here — the DEVICE Dashboard owns it —
        but the hook is preserved so an existing caller never breaks.)"""
        self._nav_top.addWidget(widget)

    def set_badge(self, key: str, count: int) -> None:
        """Set a destination's live count badge (hidden at 0)."""
        dest = self._destinations.get(key)
        if dest is not None:
            dest.set_count(count)

    def set_content(self, widget: QWidget) -> None:
        """Set the central content widget (replaces any previous). Inserted ABOVE the primary-action
        bar so a docked Start/Stop sits at the bottom of the content column (an expanding surface —
        the real case — pushes the bar flush to the bottom edge)."""
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
        """Set a top-bar device-truth field (link/battery/sd/gps/task/armed). Empty hides it. The
        'armed' field drives the SAFE/ARMED lamp (ARMED/ARMING => red/amber lamp; empty => SAFE)."""
        if key == "armed":
            self._status["armed"].setText(text)   # keep the hidden truth label in sync
            self._render_safe_lamp(text)
            return
        lbl = self._status.get(key)
        if lbl is None:
            return
        lbl.setText(text)
        lbl.setVisible(bool(text))
        lbl.setStyleSheet(f"color:{color or C.TEXT_MUTED}; padding:0 6px; font-size:11.5px;")

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
        """Add a host widget to the top status bar (before the SAFE lamp + icons) — so chrome that
        used to live on a second bottom status bar folds into this one shell bar."""
        # Insert just before the SAFE lamp (kept near the end): find the lamp's index, insert ahead.
        idx = self._status_bar_layout.indexOf(self._safe_lamp)
        if idx < 0:
            idx = self._status_bar_layout.count()
        self._status_bar_layout.insertWidget(idx, widget)

    def set_breadcrumb(self, verb: str, leaf: str = "") -> None:
        """Set the top-bar breadcrumb: ``VERB ▸ Leaf`` (the verb bolded, the leaf muted)."""
        verb = (verb or "").strip()
        leaf = (leaf or "").strip()
        self._crumb_verb = verb
        self._crumb.setText(f"<b style='color:{C.TEXT_PRIMARY}'>{verb}</b>"
                            + (f" ▸ {leaf}" if leaf else ""))

    def set_breadcrumb_leaf(self, leaf: str) -> None:
        """Update just the leaf, keeping the current verb (a host wires this to the active sub-tab)."""
        self.set_breadcrumb(getattr(self, "_crumb_verb", ""), leaf)

    def _sync_crumb(self, key: str) -> None:
        dest = self._destinations.get(key)
        if dest is not None:
            self.set_breadcrumb(dest._label, "")

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
        """Manual toggle between the full sidebar and the icon rail."""
        self.set_nav_mode("sidebar" if self._nav_mode != "sidebar" else "rail")

    @property
    def posture(self) -> str:
        return self._posture

    def set_posture(self, posture: str) -> None:
        """Set the display posture (host-driven; a plain indicator that gates nothing)."""
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
    def _render_safe_lamp(self, armed_text: str) -> None:
        """Render the top-bar state lamp from the 'armed' device-truth: empty => SAFE (green), ARMED
        => red, anything else (ARMING/pending) => amber. The lamp is display truth; the real send-path
        floor is safety.classify + the OPERATE two-factor arm."""
        t = (armed_text or "").strip().upper()
        if t.startswith("ARMED"):
            label, col, bg, bd = "ARMED", C.ERROR, "#2b1416", "#8b2c26"
        elif t:  # ARMING / pending / any non-empty non-armed state
            label, col, bg, bd = t, C.ALERT, "#241c07", "#8a6100"
        else:
            label, col, bg, bd = "SAFE", C.SUCCESS, "#0f2417", "#238636"
        self._safe_lamp.setText(f"●  {label}")
        self._safe_lamp.setStyleSheet(
            f"QLabel{{color:{col}; border:1px solid {bd}; border-radius:20px; background:{bg};"
            f" padding:3px 11px; font-weight:650; font-size:11.5px; letter-spacing:0.4px;}}")

    def _render_posture(self) -> None:
        offense = self._posture == POSTURE_OFFENSE
        self._posture_lbl.setText("Offense" if offense else "Recon / Defense")
        col = C.ALERT if offense else C.SUCCESS
        self._posture_lbl.setStyleSheet(
            f"QLabel{{color:{col}; border:1px solid {col}; border-radius:4px;"
            f" padding:2px 8px;}}")

    def _on_omnibar(self) -> None:
        text = self._omnibar.text().strip()
        if text:
            self.omnibar_submitted.emit(text)
