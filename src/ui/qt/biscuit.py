"""Biscuit-style operation widgets — the reusable card→detail pattern (A2).

Distilled from the Biscuit Manager teardown (`command-center/projects/cc-app/BISCUIT-UX-TEARDOWN-
2026-07-21.md`): every operation is one CARD (icon + title + one-line description + chevron)
DETAIL view with a big Start/Stop, an optional Mode segment, a live stat grid, and a Help sheet
opening a DETAIL view (What-it-does / Modes / Statistics / Tips). Honest, discoverable control.

These are presentation widgets: behavior is wired by the host surface via signals (start_requested /
stop_requested / mode_changed / help_requested). Pure Qt, offscreen-testable, styled from theme
tokens (LxveLabs: ACCENT purple + SUCCESS green; ERROR red = stop). Point-sized fonts (DPI-safe).
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.ui.qt.theme import colors as C


class OperationCard(QFrame):
    """One operation as a list card: a square icon tile + bold title + a dimmed one-line
    description + a chevron. Clicking (or Enter/Space when focused) emits :attr:`activated`."""

    activated = pyqtSignal()

    def __init__(self, icon: str, title: str, description: str = "",
                 parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self.setObjectName("op_card")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setStyleSheet(
            f"QFrame#op_card {{ background:{C.BG_CARD}; border:1px solid {C.BORDER};"
            f" border-radius:12px; }}"
            f"QFrame#op_card:hover {{ border:1px solid {C.ACCENT}; }}"
            f"QFrame#op_card:focus {{ border:1px solid {C.ACCENT_BRIGHT}; }}"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(12)

        tile = QLabel(icon or "•")
        tile.setAlignment(Qt.AlignCenter)
        tile.setFixedSize(40, 40)
        tile.setStyleSheet(
            f"background:{C.BG_INPUT}; color:{C.ACCENT}; border-radius:8px; font-size:15pt;")
        row.addWidget(tile)

        text = QVBoxLayout()
        text.setSpacing(1)
        self._title = QLabel(title)
        self._title.setStyleSheet(f"color:{C.TEXT_PRIMARY}; font-weight:bold; font-size:11pt;")
        text.addWidget(self._title)
        if description:
            desc = QLabel(description)
            desc.setWordWrap(True)
            desc.setStyleSheet(f"color:{C.TEXT_MUTED}; font-size:9pt;")
            text.addWidget(desc)
        row.addLayout(text, 1)

        chevron = QLabel("›")
        chevron.setStyleSheet(f"color:{C.TEXT_MUTED}; font-size:16pt;")
        row.addWidget(chevron)

    def mousePressEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        if ev.button() == Qt.LeftButton:
            self.activated.emit()
        super().mousePressEvent(ev)

    def keyPressEvent(self, ev) -> None:  # noqa: N802 (Qt override) — activate like a button
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.activated.emit()
        else:
            super().keyPressEvent(ev)


class StatTile(QFrame):
    """A color-outlined tile: a big value over a small dimmed label (the live stat-grid unit)."""

    def __init__(self, label: str, value: str = "—", color: str = "",
                 parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self.setObjectName("stat_tile")
        self._color = color or C.TEXT_PRIMARY
        self.setStyleSheet(
            f"QFrame#stat_tile {{ background:{C.BG_SURFACE}; border:1px solid {C.BORDER}; "
            f"border-radius:8px; }}")
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(1)
        self._value = QLabel(value)
        self._value.setStyleSheet(f"color:{self._color}; font-weight:bold; font-size:14pt;")
        v.addWidget(self._value)
        cap = QLabel(label)
        cap.setStyleSheet(f"color:{C.TEXT_MUTED}; font-size:8pt;")
        v.addWidget(cap)

    def set_value(self, value: str, color: str = "") -> None:
        self._value.setText(str(value))
        if color:
            self._color = color
            self._value.setStyleSheet(f"color:{color}; font-weight:bold; font-size:14pt;")


class StatGrid(QWidget):
    """A responsive grid of :class:`StatTile` (Biscuit live stat grid). Build once with the
    ordered labels; call :meth:`set_stats` each tick with ``{label: value | (value, color)}``."""

    def __init__(self, labels, columns: int = 3, parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._tiles: "dict[str, StatTile]" = {}
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        for i, label in enumerate(labels):
            tile = StatTile(label)
            self._tiles[label] = tile
            grid.addWidget(tile, i // columns, i % columns)

    def set_stats(self, stats: dict) -> None:
        for label, val in stats.items():
            tile = self._tiles.get(label)
            if tile is None:
                continue
            if isinstance(val, tuple):
                tile.set_value(val[0], val[1] if len(val) > 1 else "")
            else:
                tile.set_value(val)


class ModeSegment(QWidget):
    """A segmented control (exclusive pills). Emits :attr:`mode_changed` with the chosen mode."""

    mode_changed = pyqtSignal(str)

    def __init__(self, modes, parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._buttons: "dict[str, QPushButton]" = {}
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        for i, mode in enumerate(modes):
            btn = QPushButton(mode)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setChecked(i == 0)
            btn.setStyleSheet(
                f"QPushButton {{ background:{C.BG_INPUT}; color:{C.TEXT_MUTED}; border:none; "
                f"border-radius:8px; padding:5px 12px; font-size:9pt; }}"
                f"QPushButton:checked {{ background:{C.ACCENT}; color:{C.BG_DEEP};"
                f" font-weight:bold; }}")
            btn.clicked.connect(lambda _=False, m=mode: self._select(m))
            self._buttons[mode] = btn
            row.addWidget(btn)
        row.addStretch(1)
        self._current = modes[0] if modes else ""

    def _select(self, mode: str) -> None:
        for m, btn in self._buttons.items():
            btn.setChecked(m == mode)
        if mode != self._current:
            self._current = mode
            self.mode_changed.emit(mode)

    def current_mode(self) -> str:
        return self._current


class HelpSheet(QDialog):
    """The per-operation Help sheet: a big title, a plain-language paragraph, then labelled
    sections of icon+bold+desc rows (What It Does / Modes / Statistics) + numbered Tips. From a
    spec dict so any operation can describe itself honestly:

        {"title": str, "summary": str,
         "what_it_does": [(icon, name, desc), ...],
         "modes": [(icon, name, desc), ...],       # optional
         "statistics": [(icon, name, desc), ...],  # optional
         "tips": [str, ...]}                        # optional numbered tips
    """

    def __init__(self, spec: dict, parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(spec.get("title", "Help"))
        self.setMinimumWidth(440)
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setSpacing(10)

        title = QLabel(spec.get("title", "Help"))
        title.setStyleSheet(f"color:{C.TEXT_PRIMARY}; font-weight:bold; font-size:15pt;")
        v.addWidget(title)
        if spec.get("summary"):
            summ = QLabel(spec["summary"])
            summ.setWordWrap(True)
            summ.setStyleSheet(f"color:{C.TEXT_MUTED}; font-size:10pt;")
            v.addWidget(summ)

        for key, heading in (("what_it_does", "What It Does"), ("modes", "Modes"),
                             ("statistics", "Statistics")):
            rows = spec.get(key)
            if rows:
                v.addWidget(self._section(heading, rows))

        tips = spec.get("tips")
        if tips:
            v.addWidget(self._heading("Usage Tips"))
            for i, tip in enumerate(tips, 1):
                lbl = QLabel(f"{i}.  {tip}")
                lbl.setWordWrap(True)
                lbl.setStyleSheet(f"color:{C.TEXT_MUTED}; font-size:9pt;")
                v.addWidget(lbl)

        v.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        outer.addWidget(close)

    @staticmethod
    def _heading(text: str) -> QLabel:
        h = QLabel(text)
        h.setStyleSheet(f"color:{C.ACCENT}; font-weight:bold; font-size:11pt;")
        return h

    def _section(self, heading: str, rows) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        v.addWidget(self._heading(heading))
        for icon, name, desc in rows:
            card, layout = _row_card()
            top = QLabel(f"{icon}  {name}")
            top.setStyleSheet(f"color:{C.TEXT_PRIMARY}; font-weight:bold; font-size:10pt;")
            layout.addWidget(top)
            d = QLabel(desc)
            d.setWordWrap(True)
            d.setStyleSheet(f"color:{C.TEXT_MUTED}; font-size:9pt;")
            layout.addWidget(d)
            v.addWidget(card)
        return box


def _row_card() -> "tuple[QFrame, QVBoxLayout]":
    card = QFrame()
    card.setStyleSheet(
        f"background:{C.BG_CARD}; border:1px solid {C.BORDER}; border-radius:8px;")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(10, 8, 10, 8)
    layout.setSpacing(2)
    return card, layout


class OperationDetail(QWidget):
    """The operation detail view: a header (title + Help ?), an optional Mode segment, a stat grid,
    a big Start/Stop pill, and a status line. Behavior is the host's — it emits signals and reflects
    state via :meth:`set_running` / :meth:`set_ready`.

    :param help_spec: optional dict for a :class:`HelpSheet` (shows a ``?`` button when present).
    """

    start_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    mode_changed = pyqtSignal(str)
    help_requested = pyqtSignal()

    def __init__(self, title: str, *, stat_labels=None, modes=None,
                 help_spec: "Optional[dict]" = None,
                 parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._running = False
        self._ready = True
        self._help_spec = help_spec
        root = QVBoxLayout(self)
        root.setSpacing(10)

        head = QHBoxLayout()
        heading = QLabel(title)
        heading.setStyleSheet(f"color:{C.TEXT_PRIMARY}; font-weight:bold; font-size:13pt;")
        head.addWidget(heading)
        head.addStretch(1)
        if help_spec is not None:
            btn_help = QPushButton("?")
            btn_help.setFixedSize(28, 28)
            btn_help.setCursor(Qt.PointingHandCursor)
            btn_help.setToolTip("What this does")
            btn_help.setStyleSheet(
                f"QPushButton {{ background:{C.BG_INPUT}; color:{C.ACCENT}; border-radius:14px; "
                f"font-weight:bold; }}")
            btn_help.clicked.connect(self._on_help)
            head.addWidget(btn_help)
        root.addLayout(head)

        self._mode_segment = None
        if modes:
            self._mode_segment = ModeSegment(modes)
            self._mode_segment.mode_changed.connect(self.mode_changed)
            root.addWidget(self._mode_segment)

        self._stats = None
        if stat_labels:
            self._stats = StatGrid(stat_labels)
            root.addWidget(self._stats)

        self._status = QLabel("Ready")
        self._status.setStyleSheet(f"color:{C.TEXT_MUTED}; font-size:9pt;")
        root.addWidget(self._status)

        self._btn = QPushButton("Start")
        self._btn.setMinimumHeight(40)
        self._btn.setCursor(Qt.PointingHandCursor)
        self._btn.clicked.connect(self._on_click)
        root.addWidget(self._btn)
        self._paint_button()

    # ── host API ──────────────────────────────────────────────────────
    def set_stats(self, stats: dict) -> None:
        if self._stats is not None:
            self._stats.set_stats(stats)

    def current_mode(self) -> str:
        return self._mode_segment.current_mode() if self._mode_segment is not None else ""

    def set_running(self, running: bool, status: str = "") -> None:
        """Reflect the real operation state (the host sets this after it actually starts/stops)."""
        self._running = bool(running)
        self._status.setText(status or ("Running" if running else "Ready"))
        self._paint_button()

    def set_ready(self, ready: bool, reason: str = "") -> None:
        """Enable/disable Start with honest guidance (e.g. 'Select a target first'). While running,
        the button stays enabled so Stop is always reachable."""
        self._ready = bool(ready)
        if not ready and reason:
            self._status.setText(reason)
        self._paint_button()

    # ── internals ─────────────────────────────────────────────────────
    def _on_click(self) -> None:
        # Optimistic: emit intent; the host confirms real state via set_running.
        (self.stop_requested if self._running else self.start_requested).emit()

    def _on_help(self) -> None:
        self.help_requested.emit()
        if self._help_spec is not None:
            HelpSheet(self._help_spec, self).exec_()

    def _paint_button(self) -> None:
        if self._running:
            self._btn.setText("Stop")
            self._btn.setEnabled(True)
            bg, fg = C.ERROR, "#ffffff"
        else:
            self._btn.setText("Start")
            self._btn.setEnabled(self._ready)
            bg, fg = (C.SUCCESS, C.BG_DEEP) if self._ready else (C.BG_INPUT, C.TEXT_DISABLED)
        self._btn.setStyleSheet(
            f"QPushButton {{ background:{bg}; color:{fg}; border:none; border-radius:10px; "
            f"font-weight:bold; font-size:11pt; }}")
