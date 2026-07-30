"""OPERATE HOME screen — a LAUNCHER, not a browser.

The domain grid is the only content. Every tile either NAVIGATES to that capability's real surface
(Wi-Fi/BLE -> the analyzer, Tools -> Crack Lab, Settings -> the Settings tab; the host routes
``navigate_requested``) or is a greyed **P4 roadmap tile** (GPS-Wardrive / RF-Sub-GHz, which land in
MAP at P4). There is no in-place read-only browser: a domain's real work happens in its own surface,
never a transmit-nothing clone here.

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
from src.ui.qt.theme import colors as C


class _HomeSummary(QWidget):
    """The Operate Home landing header (slice E): a one-line session state-line + a compact metric
    strip (devices / targets / captures / armed). Dumb display — a host pushes real hub counts via
    ``set_summary``; this widget invents nothing."""

    def __init__(self, parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._state = QLabel("")
        self._state.setObjectName("home_state_line")
        # A grounded bit of session value the always-visible status bar does NOT show: the most
        # recent capture (host pushes it from the real capture store; empty until one is logged).
        self._last = QLabel("")
        self._last.setObjectName("home_last_capture")
        self._metrics: dict[str, QLabel] = {}
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 6, 10, 6)
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
        # State-line: a plain factual readout (not hero copy) — connect prompt or the device tally.
        if d == 0:
            self._state.setText("No device connected — connect a board to begin")
        else:
            self._state.setText(f"{d} device" + ("" if d == 1 else "s") + " connected")
        lc = (last_capture or "").strip()
        self._last.setText(f"·  Last capture: {lc}" if lc else "")


class OperateHome(QWidget):
    """The OPERATE HOME launcher: a landing summary + the domain tile grid. Every tile navigates to
    its real surface (``navigate_requested``) or is a greyed P4 roadmap tile — no browser."""

    navigate_requested = pyqtSignal(str)   # a navigable tile asks the host to open its real surface

    def __init__(self, external_domains: "Optional[set[str]]" = None,
                 parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        # ``external_domains`` is accepted for call-site compatibility; every non-roadmap tile now
        # navigates, so there is no separate "external" set to track — the host routes each intent.
        self._grid = DomainGrid()
        self._grid.domain_selected.connect(self.show_domain)
        self._summary = _HomeSummary()   # landing header; a host feeds it real hub counts
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._summary)
        outer.addWidget(self._grid)

    def set_summary(self, devices: int, targets: int, captures: int, armed: str,
                    last_capture: str = "") -> None:
        """Push real hub counts into the landing summary (host-driven; OperateHome invents none)."""
        self._summary.set_summary(devices, targets, captures, armed, last_capture)

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
