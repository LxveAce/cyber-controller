"""OPERATE HOME screen (design brief) — the domain tile grid + the selected domain's detail.

Composes :class:`DomainGrid` (the radio/domain tiles) with a ``QStackedWidget``: tapping a tile
shows that domain's screen, a Home action returns to the grid. Wi-Fi / BLE / GPS / Sub-GHz open
their real domain views; a domain with no dedicated screen yet shows an honest "coming soon"
placeholder. A domain listed in ``external_domains`` has NO in-place screen — tapping it emits
``navigate_requested`` so the host switches to that capability's real tab (Tools -> Crack Lab,
Settings -> the Settings tab) instead of a placeholder that lies about an already-shipped feature.
Self-contained: it emits an intent and never reaches for the tab structure itself.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.ui.qt.ble_domain import BleDomainView
from src.ui.qt.domain_grid import DomainGrid
from src.ui.qt.gps_domain import GpsDomainView
from src.ui.qt.subghz_domain import SubGhzDomainView
from src.ui.qt.theme import colors as C
from src.ui.qt.wifi_domain import WifiDomainView


class _HomeSummary(QWidget):
    """The Operate Home landing header (slice E): a one-line session state-line + a compact metric
    strip (devices / targets / captures / armed). Dumb display — a host pushes real hub counts via
    ``set_summary``; this widget invents nothing. Shown only on the grid landing (the header the
    domain grid lives under), hidden inside a domain screen."""

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


def _placeholder(title: str) -> QWidget:
    w = QWidget()
    v = QVBoxLayout(w)
    lbl = QLabel(f"{title} — coming soon")
    lbl.setObjectName("domain_placeholder")
    v.addStretch(1)
    v.addWidget(lbl)
    v.addStretch(1)
    return w


class OperateHome(QWidget):
    """The OPERATE HOME shell: domain grid ⇄ per-domain screen on a stack."""

    home_shown = pyqtSignal()       # returned to the grid
    navigate_requested = pyqtSignal(str)  # external-domain tile asks the host to open its tab

    def __init__(self, wifi_center: "Optional[QWidget]" = None,
                 ble_center: "Optional[QWidget]" = None,
                 gps_center: "Optional[QWidget]" = None,
                 subghz_center: "Optional[QWidget]" = None,
                 external_domains: "Optional[set[str]]" = None,
                 parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._current: Optional[str] = None
        # Domains that live in a real tab elsewhere: no in-place screen is built for them.
        # Tapping the tile emits navigate_requested for the host to open that tab (see docstring).
        self._external: set[str] = set(external_domains or ())
        self._grid = DomainGrid()
        self._stack = QStackedWidget()
        self._stack.addWidget(self._grid)  # index 0 = home

        # One screen per domain: Wi-Fi and BLE reuse the SAME three-panel frame (proving it is
        # general); the remaining domains are honest placeholders until their screens land. An
        # external domain gets no screen at all — it navigates the host instead.
        self._views: dict[str, QWidget] = {}
        titles = {k: t for k, _i, t, _d in _domain_titles()}
        for key in self._grid.domain_keys():
            if key in self._external:
                continue
            if key == "wifi":
                view: QWidget = WifiDomainView(center=wifi_center or QWidget())
            elif key == "ble":
                view = BleDomainView(center=ble_center or QWidget())
            elif key == "gps":
                view = GpsDomainView(center=gps_center or QWidget())
            elif key == "subghz":
                view = SubGhzDomainView(center=subghz_center or QWidget())
            else:
                view = _placeholder(titles.get(key, key))
            self._views[key] = view
            self._stack.addWidget(view)
        self._grid.domain_selected.connect(self.show_domain)

        self._btn_home = QPushButton("← Domains")
        self._btn_home.clicked.connect(self.show_home)
        bar = QHBoxLayout()
        bar.addWidget(self._btn_home)
        bar.addStretch(1)

        # Slice E: the landing header (state-line + metric strip). Shown on the grid landing, hidden
        # inside a domain screen (the inverse of _btn_home). A host feeds it real hub counts.
        self._summary = _HomeSummary()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._summary)
        outer.addLayout(bar)
        outer.addWidget(self._stack)
        self.show_home()

    def set_summary(self, devices: int, targets: int, captures: int, armed: str,
                    last_capture: str = "") -> None:
        """Push real hub counts into the landing summary (host-driven; OperateHome invents none)."""
        self._summary.set_summary(devices, targets, captures, armed, last_capture)

    def show_home(self) -> None:
        self._current = None
        self._stack.setCurrentWidget(self._grid)
        self._btn_home.setVisible(False)   # nothing to go back from on the grid itself
        self._summary.setVisible(True)     # the landing header belongs to the grid view
        self.home_shown.emit()

    def show_domain(self, key: str) -> None:
        if key in self._external:
            # No in-place screen: ask the host to open this capability's real tab. Stay on the grid.
            self.navigate_requested.emit(key)
            return
        view = self._views.get(key)
        if view is None:
            return
        self._current = key
        self._stack.setCurrentWidget(view)
        self._btn_home.setVisible(True)
        self._summary.setVisible(False)    # give the domain screen the full height

    def current_domain(self) -> "Optional[str]":
        return self._current

    def domain_view(self, key: str) -> "Optional[QWidget]":
        return self._views.get(key)


def _domain_titles():
    # Import lazily to avoid a hard import cycle and to reuse DomainGrid's single source of truth.
    from src.ui.qt.domain_grid import _DOMAINS
    return _DOMAINS


def build_operate_home(external_domains: "Optional[set[str]]" = None,
                       parent: "Optional[QWidget]" = None) -> "OperateHome":
    """Build an :class:`OperateHome` for the app shell.

    Spade v2 (P2c): Wi-Fi and BLE are **external** domains — a tap navigates to the real analyzer
    (in the Analyze surface) instead of embedding a FRESH duplicate double-fed from the same event
    taps (the old transmit-nothing clones + orphan-tap crash risk are gone). The caller passes any
    other external tiles (e.g. ``{"tools", "settings"}``); ``wifi``/``ble`` are always added.
    Returns just the :class:`OperateHome` (no centers — the shell routes ``navigate_requested``).
    """
    ext = set(external_domains or set()) | {"wifi", "ble"}
    return OperateHome(external_domains=ext, parent=parent)
