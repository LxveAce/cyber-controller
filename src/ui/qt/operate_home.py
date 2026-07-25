"""OPERATE HOME screen (design brief) — the domain tile grid + the selected domain's detail.

Composes :class:`DomainGrid` (the radio/domain tiles) with a ``QStackedWidget``: tapping a tile
shows that domain's screen, a Home action returns to the grid. Wi-Fi opens the real three-panel
:class:`WifiDomainView`; the other domains show an honest "coming soon" placeholder until their
screens land. Self-contained so the app shell embeds THIS one widget — no tab-structure coupling.
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

from src.ui.qt.domain_grid import DomainGrid
from src.ui.qt.wifi_domain import WifiDomainView


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

    domain_shown = pyqtSignal(str)  # a domain screen was opened (key)
    home_shown = pyqtSignal()       # returned to the grid

    def __init__(self, wifi_center: "Optional[QWidget]" = None,
                 parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._current: Optional[str] = None
        self._grid = DomainGrid()
        self._stack = QStackedWidget()
        self._stack.addWidget(self._grid)  # index 0 = home

        # One screen per domain: Wi-Fi is the real three-panel; the rest are honest placeholders.
        self._views: dict[str, QWidget] = {}
        titles = {k: t for k, _i, t, _d in _domain_titles()}
        for key in self._grid.domain_keys():
            view = WifiDomainView(center=wifi_center or QWidget()) if key == "wifi" \
                else _placeholder(titles.get(key, key))
            self._views[key] = view
            self._stack.addWidget(view)
        self._grid.domain_selected.connect(self.show_domain)

        self._btn_home = QPushButton("← Domains")
        self._btn_home.clicked.connect(self.show_home)
        bar = QHBoxLayout()
        bar.addWidget(self._btn_home)
        bar.addStretch(1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(bar)
        outer.addWidget(self._stack)
        self.show_home()

    def show_home(self) -> None:
        self._current = None
        self._stack.setCurrentWidget(self._grid)
        self._btn_home.setVisible(False)  # nothing to go back from on the grid itself
        self.home_shown.emit()

    def show_domain(self, key: str) -> None:
        view = self._views.get(key)
        if view is None:
            return
        self._current = key
        self._stack.setCurrentWidget(view)
        self._btn_home.setVisible(True)
        self.domain_shown.emit(key)

    def current_domain(self) -> "Optional[str]":
        return self._current

    def domain_view(self, key: str) -> "Optional[QWidget]":
        return self._views.get(key)


def _domain_titles():
    # Import lazily to avoid a hard import cycle and to reuse DomainGrid's single source of truth.
    from src.ui.qt.domain_grid import _DOMAINS
    return _DOMAINS
