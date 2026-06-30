"""Detachable / pop-out tabs.

Any tab can pop out into its own resizable, top-level window — drag it to a second monitor, resize it
freely — and re-dock seamlessly back onto the tab strip, or just close it (closing re-docks by default so
a working panel is never lost). This is the foundation for the per-firmware "Device View" pop-outs and for
multi-monitor cyberdeck ops.

Design (see command-center/projects/cc-device-view-PLAN.md §2): a thin ``DetachableTabWidget(QTabWidget)``
plus a ``PopoutWindow(QWidget)``. The rest of the app is untouched — and because the app navigates tabs by
*widget reference* (``setCurrentWidget(self._flash_tab)``), removing/re-inserting a page never breaks a
stored reference. Works under ``QT_QPA_PLATFORM=offscreen`` so it stays unit-testable with no display.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)


class PopoutWindow(QWidget):
    """A top-level window hosting a torn-off tab page.

    It is parentless (a real OS window, so it can live on any monitor). Closing it re-docks the page by
    default rather than destroying it; a true close is only done programmatically via :meth:`force_close`.
    """

    redock_requested = pyqtSignal(object)  # emits the hosted page widget

    def __init__(self, page: QWidget, tab_text: str, home_index: int) -> None:
        super().__init__()  # no parent => independent top-level window
        self.page = page
        self.tab_text = tab_text
        self.home_index = home_index
        self._force = False

        self.setObjectName("popout_window")
        self.setWindowTitle(f"Cyber Controller — {tab_text}")
        try:
            self.setWindowIcon(page.window().windowIcon())
        except Exception:  # noqa: BLE001
            pass

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Slim header: title + Re-dock button (the OS title-bar close also re-docks).
        header = QWidget()
        header.setObjectName("popout_header")
        header.setStyleSheet(
            "#popout_header{background:#161b22;border-bottom:1px solid #30363d;}"
            "#popout_header QLabel{color:#8b949e;font-size:9pt;}"
        )
        hl = QHBoxLayout(header)
        hl.setContentsMargins(10, 5, 6, 5)
        hl.setSpacing(8)
        hl.addWidget(QLabel(f"❐  {tab_text}"))
        hl.addStretch(1)
        redock = QPushButton("⤓ Re-dock")
        redock.setToolTip("Return this panel to the main window (or just close the window)")
        redock.setStyleSheet("font-size:9pt;padding:3px 10px;")
        redock.clicked.connect(self._emit_redock)
        hl.addWidget(redock)
        outer.addWidget(header)

        # The reparented page.
        page.setParent(self)
        outer.addWidget(page, 1)
        page.show()

        # Sensible starting size: the page's own hint, with a small floor.
        hint = page.sizeHint()
        self.resize(max(hint.width(), 320), max(hint.height() + 36, 240))

    def _emit_redock(self) -> None:
        self.redock_requested.emit(self.page)

    def force_close(self) -> None:
        """Truly close the window (used by the owner after the page has been reparented out)."""
        self._force = True
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # Default behavior: re-dock instead of destroying the panel. The owner reparents the page back
        # and then calls force_close(), which sets self._force so this second close actually proceeds.
        if self._force:
            event.accept()
            return
        event.ignore()
        self._emit_redock()


class DetachableTabWidget(QTabWidget):
    """A QTabWidget whose tabs can pop out into :class:`PopoutWindow`s and re-dock.

    Drop-in for ``QTabWidget``. Adds: a corner pop-out button, double-click-to-detach, a tab-bar
    right-click "Pop out", and :meth:`detach_current` (wire a shortcut to it). Tracks open pop-outs and
    can persist/restore which tabs are detached across sessions.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._popouts: "dict[QWidget, PopoutWindow]" = {}
        self.setMovable(True)

        # Corner button to pop out the current tab.
        corner = QPushButton("⇱")
        corner.setToolTip("Pop out the current tab into its own window")
        corner.setFixedSize(26, 22)
        corner.setStyleSheet("font-size:11pt;padding:0;")
        corner.clicked.connect(self.detach_current)
        self.setCornerWidget(corner, Qt.TopRightCorner)

        # Double-click a tab to detach it.
        self.tabBarDoubleClicked.connect(self.detach_index)

        # Right-click the tab bar -> "Pop out".
        bar = self.tabBar()
        bar.setContextMenuPolicy(Qt.CustomContextMenu)
        bar.customContextMenuRequested.connect(self._tab_context_menu)

    # ── detach ───────────────────────────────────────────────────────
    def detach_current(self) -> Optional[PopoutWindow]:
        return self.detach_index(self.currentIndex())

    def detach_index(self, index: int) -> Optional[PopoutWindow]:
        if index < 0 or index >= self.count():
            return None
        page = self.widget(index)
        if page is None or page in self._popouts:
            return None
        tab_text = self.tabText(index)
        self.removeTab(index)

        win = PopoutWindow(page, tab_text, index)
        win.redock_requested.connect(self._redock_page)
        self._popouts[page] = win
        win.show()
        win.raise_()
        win.activateWindow()
        log.info("Detached tab %r into a pop-out window", tab_text)
        return win

    # ── re-dock ──────────────────────────────────────────────────────
    def _redock_page(self, page: QWidget) -> None:
        win = self._popouts.pop(page, None)
        if win is None:
            return
        index = min(win.home_index, self.count())
        page.setParent(None)
        self.insertTab(index, page, win.tab_text)
        self.setCurrentWidget(page)
        win.force_close()
        log.info("Re-docked tab %r", win.tab_text)

    def redock_all(self) -> None:
        for page in list(self._popouts):
            self._redock_page(page)

    def close_all_popouts(self) -> None:
        """Re-dock every pop-out (used on app shutdown so no orphan windows linger)."""
        self.redock_all()

    # ── context menu ─────────────────────────────────────────────────
    def _tab_context_menu(self, pos) -> None:
        bar = self.tabBar()
        index = bar.tabAt(pos)
        if index < 0:
            return
        menu = QMenu(self)
        act = menu.addAction("Pop out")
        chosen = menu.exec_(bar.mapToGlobal(pos))
        if chosen is act:
            self.detach_index(index)

    # ── persistence ──────────────────────────────────────────────────
    def detached_state(self) -> str:
        """JSON {tab_label: window_geometry_hex} for the currently-detached tabs (for QSettings)."""
        state = {}
        for page, win in self._popouts.items():
            try:
                state[win.tab_text] = bytes(win.saveGeometry()).hex()
            except Exception:  # noqa: BLE001
                continue
        return json.dumps(state)

    def restore_detached(self, state_json: str) -> None:
        """Re-detach the tabs named in ``state_json`` and restore their window geometry. Never raises."""
        try:
            state = json.loads(state_json) if state_json else {}
        except Exception:  # noqa: BLE001
            return
        if not isinstance(state, dict):
            return
        for label, geom_hex in state.items():
            index = self._index_of_label(label)
            if index < 0:
                continue
            win = self.detach_index(index)
            if win is None:
                continue
            try:
                from PyQt5.QtCore import QByteArray
                win.restoreGeometry(QByteArray(bytes.fromhex(geom_hex)))
            except Exception:  # noqa: BLE001
                pass

    def _index_of_label(self, label: str) -> int:
        for i in range(self.count()):
            if self.tabText(i) == label:
                return i
        return -1
