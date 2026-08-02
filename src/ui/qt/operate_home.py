"""OPERATE HOME screen — a one-tap EXECUTION surface, not a browser (WS3).

Three zones (top to bottom): Zone A ``_HomeSummary`` — a read-only status header with a
connection/health pill; Zone B ``QuickActionsStrip`` — the hero row of curated one-tap actions for
the connected firmware, each riding CC's existing guarded ``_send``; Zone C ``DomainGrid`` — the
demoted "Go deeper" nav row. Every Zone C tile NAVIGATES to that capability's real surface
(Wi-Fi/BLE -> the analyzer, Tools -> Crack Lab, Settings -> the Settings tab; the host routes
``navigate_requested``) or is a greyed **P4 roadmap tile** (GPS-Wardrive / RF-Sub-GHz, which land in
MAP at P4). There is no in-place read-only browser: a domain's real work happens in its own surface,
never a transmit-nothing clone here. Zone B is host-fed via :meth:`OperateHome.set_actions` on
connect / firmware-change only; readiness is refreshed cheaply on the poll.

Spade D6c retired the old ``DomainDetailView`` browser + the per-domain ``QStackedWidget``: honest
functionality is now structural — a tile can only navigate to something real, or honestly say where
it's going. Self-contained: it emits an intent and never reaches for the tab structure itself.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from src.ui.qt.domain_grid import DomainGrid
from src.ui.qt.quick_actions_strip import QuickActionsStrip
from src.ui.qt.theme import colors as C


class _HomeSummary(QWidget):
    """The Operate Home landing header (slice E): a one-line session state-line + a compact metric
    strip (devices / targets / captures / armed). Dumb display — a host pushes real hub counts via
    ``set_summary``; this widget invents nothing."""

    def __init__(self, parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        # Connection/health pill (WS3 Zone A): green connected / amber arming / grey disconnected.
        # The loudest, first thing on the line (WiGLE's rule) and the chip that never collapses.
        self._pill = QLabel("")
        self._pill.setObjectName("home_conn_pill")
        self._state = QLabel("")
        self._state.setObjectName("home_state_line")
        # A grounded bit of session value the always-visible status bar does NOT show: the most
        # recent capture (host pushes it from the real capture store; empty until one is logged).
        self._last = QLabel("")
        self._last.setObjectName("home_last_capture")
        self._metrics: dict[str, QLabel] = {}
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 6, 10, 6)
        row.addWidget(self._pill)
        row.addWidget(self._state)
        row.addWidget(self._last)
        row.addStretch(1)
        for key in ("devices", "targets", "captures", "armed"):
            lbl = QLabel("")
            lbl.setObjectName(f"home_metric_{key}")
            self._metrics[key] = lbl
            row.addWidget(lbl)
        self.set_summary(0, 0, 0, "")

    def set_summary(self, devices: int, targets: int, captures: int, armed: str,
                    last_capture: str = "") -> None:
        """Update the state-line + metric chips from real counts (a host pushes these).
        ``last_capture`` is the most-recent capture's label (grounded, may be empty)."""
        d, t, c = max(0, int(devices)), max(0, int(targets)), max(0, int(captures))
        self._metrics["devices"].setText(f"{d} device" + ("" if d == 1 else "s"))
        self._metrics["targets"].setText(f"{t} target" + ("" if t == 1 else "s"))
        self._metrics["captures"].setText(f"{c} capture" + ("" if c == 1 else "s"))
        state = (armed or "").strip().lower()
        armed_lbl = self._metrics["armed"]
        if state == "armed":
            armed_lbl.setText("ARMED")
            armed_lbl.setStyleSheet(f"color:{C.ERROR}; font-weight:bold;")
        elif state in ("pending", "arming"):
            armed_lbl.setText("ARMING")
            armed_lbl.setStyleSheet(f"color:{C.WARNING}; font-weight:bold;")
        else:
            armed_lbl.setText("")
            armed_lbl.setStyleSheet("")
        # Connection/health pill from the same inputs (no new data source): disconnected -> grey,
        # arming -> amber, connected -> green. It is the survivor chip in the small-screen collapse.
        if d == 0:
            self._pill.setText("○ Disconnected")
            self._pill.setStyleSheet(f"color:{C.TEXT_MUTED}; font-weight:bold;")
        elif state in ("pending", "arming"):
            self._pill.setText("● Arming")
            self._pill.setStyleSheet(f"color:{C.WARNING}; font-weight:bold;")
        else:
            self._pill.setText("● Connected")
            self._pill.setStyleSheet(f"color:{C.SUCCESS}; font-weight:bold;")
        # State-line: a plain factual readout (not hero copy) — connect prompt or the device tally.
        if d == 0:
            self._state.setText("No device connected — connect a board to begin")
        else:
            self._state.setText(f"{d} device" + ("" if d == 1 else "s") + " connected")
        lc = (last_capture or "").strip()
        self._last.setText(f"·  Last capture: {lc}" if lc else "")

    def set_compact(self, compact: bool) -> None:
        """Densify Zone A on a cramped canvas: hide the metric chips + last-capture line, keeping
        the connection pill + state line (which NEVER collapse, §5). A pure visibility toggle."""
        show = not compact
        for lbl in self._metrics.values():
            lbl.setVisible(show)
        self._last.setVisible(show)


class OperateHome(QWidget):
    """The OPERATE HOME surface: a status header (Zone A) + the one-tap QuickActionsStrip (Zone B) +
    the demoted "Go deeper" domain grid (Zone C). Each strip tap rides the guarded ``_send``; each
    grid tile navigates to its real surface (``navigate_requested``) or is a greyed roadmap tile."""

    navigate_requested = pyqtSignal(str)   # a navigable tile asks the host to open its real surface

    def __init__(self, external_domains: "Optional[set[str]]" = None,
                 parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        # ``external_domains`` is accepted for call-site compatibility; every non-roadmap tile now
        # navigates, so there is no separate "external" set to track — the host routes each intent.
        self._summary = _HomeSummary()      # Zone A: landing header (host feeds real hub counts)
        self._strip = QuickActionsStrip()   # Zone B: hero one-tap actions (fed via set_actions)
        self._deeper_lbl = QLabel("Go deeper")   # Zone C header — demotes the nav grid to secondary
        self._deeper_lbl.setObjectName("home_go_deeper")
        self._deeper_lbl.setStyleSheet(f"color:{C.TEXT_MUTED}; font-size:9pt; padding:2px 10px;")
        self._grid = DomainGrid()           # Zone C: "Go deeper" nav row (navigates, never a clone)
        self._grid.domain_selected.connect(self.show_domain)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._summary)
        outer.addWidget(self._strip)
        outer.addWidget(self._deeper_lbl)
        outer.addWidget(self._grid)
        outer.addStretch(1)
        self._last_home_size: "Optional[str]" = None   # last size class (relayout debounce)
        self._relayout_home(force=True)   # seed density / label / hit-target before the first show

    def set_summary(self, devices: int, targets: int, captures: int, armed: str,
                    last_capture: str = "") -> None:
        """Push real hub counts into the landing summary (host-driven; OperateHome invents none)."""
        self._summary.set_summary(devices, targets, captures, armed, last_capture)

    def set_actions(self, cis: "list", run_fn, send, ready_fn, safe_state_fn,
                    supports_arm: bool = False, stop_ci=None) -> None:
        """Rebuild Zone B (the quick-actions strip) for the connected firmware. Host-driven; call
        ONLY on connect / disconnect / firmware-change — never on the poll (a rebuild tears down an
        open OpPanel; MainWindow gates it on a stored (port, firmware))."""
        self._strip.set_actions(cis, run_fn, send, ready_fn, safe_state_fn, supports_arm, stop_ci)

    def refresh_readiness(self) -> None:
        """Poll-safe: refresh the strip tiles' enabled/disabled-reason (from the ~2 s poll)."""
        self._strip.refresh_readiness()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._relayout_home()

    def _relayout_home(self, force: bool = False) -> None:
        """Re-apply the Home layout on a size-class change (debounced). ``force`` runs it once at
        build so density / label / hit-target are seeded before the first show. Mirrors
        ``OperateTab._relayout_operate``."""
        from src.ui.qt.layout_profile import layout_profile, operate_home_layout
        from src.ui.qt.touch_mode import touch_active
        dpi = self.logicalDpiX() or 96
        profile = layout_profile(max(1, self.width()), max(1, self.height()),
                                 touch=touch_active(), dpi=dpi)
        if not force and profile.size == self._last_home_size:   # debounce on the size class
            return
        self._last_home_size = profile.size
        self._apply_home_layout(operate_home_layout(profile))

    def _apply_home_layout(self, hl) -> None:
        """Apply an :class:`OperateHomeLayout`: densify Zone A (hide the metric chips on compact),
        collapse the Zone C "Go deeper" label, and lift the strip tiles + STOP to the hit-target.
        The connection pill and STOP never collapse (§5)."""
        self._summary.set_compact(not hl.show_metric_chips)
        self._deeper_lbl.setVisible(hl.show_go_deeper_label)
        self._strip.set_min_target(hl.hit_edge_pt)

    def show_domain(self, key: str) -> None:
        """A tile was chosen. Roadmap tiles (gps/subghz) are greyed and can't fire; every other tile
        navigates to its real surface via the host — never an in-place clone."""
        if key in self._grid.roadmap_keys():
            return
        self.navigate_requested.emit(key)

    def current_domain(self) -> "Optional[str]":
        """A launcher never opens a domain in-place, so nothing is ever 'current' here."""
        return None

    def domain_view(self, key: str) -> "Optional[QWidget]":
        """A launcher builds no in-place domain screens — always None (an honest accessor)."""
        return None


def build_operate_home(external_domains: "Optional[set[str]]" = None,
                       parent: "Optional[QWidget]" = None) -> "OperateHome":
    """Build the Operate Home launcher for the app shell.

    Every domain tile navigates to its real surface (the host routes ``navigate_requested``) or is a
    greyed P4 roadmap tile (gps/subghz) — there is no in-place browser. Spade D6c retired the
    ``DomainDetailView`` scaffold; this returns just the :class:`OperateHome`.
    """
    return OperateHome(external_domains=external_domains, parent=parent)
