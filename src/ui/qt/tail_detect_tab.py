"""Follower / tail-detection panel (HUNT) — awareness-first counter-surveillance over the stream.

Renders CC's PersistenceTracker (src/core/tail_detect.py) as a live HUNT analyzer: a device that
keeps reappearing near you across time windows scores high and is flagged as a possible tail. Fed by
the TargetIngestor event tap (probe_request / ble_found / client_found), so it works with ANY
connected scanner. Method reused (clean reimplementation, MIT) from ArgeliusLabs/Chasing-Your-Tail.

Awareness-first + read-only: it flags "this device keeps reappearing," never "confirmed follower,"
and NEVER acts on a device — no attack, no transmit. safety.py is untouched. "Mark as mine" drops
your own device into the tracker's ignore list (persisted in Settings, filtered across sessions).
"""
from __future__ import annotations

import time
from typing import Any, Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core.tail_detect import PersistenceTracker, attach_tail_detector, tails_to_alerts
from src.ui.qt.theme import colors as C

_REFRESH_MS = 5000
_DEFAULT_THRESHOLD = 0.5
_SETTINGS_KEY = "tail_detect"


class TailDetectTab(QWidget):
    """HUNT follower/tail-detection panel over PersistenceTracker. Attaches to the ingestor stream
    on build, refreshes the flagged-device table on a ~5s timer, and marks a device as your own."""

    def __init__(self, ingestor: Any = None, tracker: "Optional[PersistenceTracker]" = None,
                 now_fn: "Optional[Any]" = None, model: Any = None,
                 parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._now = now_fn or time.time
        self._tracker = tracker or PersistenceTracker(ignore=self._load_ignore())
        self._threshold = _DEFAULT_THRESHOLD
        self._ingestor = ingestor
        # Optional shared MetricsModel (the instance the Dashboard reads): when set, each refresh
        # also routes flagged tails to canonical ALERT readings so a follower hits the alert slot.
        self._model = model
        self._observer = None
        if ingestor is not None:
            self._observer = attach_tail_detector(ingestor, self._tracker, self._now)
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.setInterval(_REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        hdr = QLabel(
            "<b>Follower / tail detection</b> — flags a device that <b>keeps reappearing</b> near "
            "you across time (possible surveillance). Awareness-first: it never confirms a "
            "follower and never acts on a device. Method: ArgeliusLabs/Chasing-Your-Tail-NG (MIT)."
        )
        hdr.setWordWrap(True)
        hdr.setStyleSheet(f"color:{C.TEXT_MUTED};")
        outer.addWidget(hdr)

        ctl = QHBoxLayout()
        ctl.addWidget(QLabel("Min persistence:"))
        self._threshold_spin = QDoubleSpinBox()
        self._threshold_spin.setRange(0.0, 1.0)
        self._threshold_spin.setSingleStep(0.1)
        self._threshold_spin.setValue(_DEFAULT_THRESHOLD)
        self._threshold_spin.setToolTip(
            "0.5 = seen in half the recent windows; 0.8 = stalking-only (near-constant presence).")
        self._threshold_spin.valueChanged.connect(self._on_threshold)
        ctl.addWidget(self._threshold_spin)
        ctl.addStretch(1)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet(f"color:{C.TEXT_MUTED};")
        ctl.addWidget(self._count_label)
        outer.addLayout(ctl)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Device", "Label", "Persistence", "Windows"])
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.verticalHeader().setVisible(False)   # drop the gray row-index gutter
        self._table.setAlternatingRowColors(True)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)            # Device
        hh.setSectionResizeMode(1, QHeaderView.Stretch)            # Label
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)   # Persistence
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)   # Windows
        outer.addWidget(self._table, 1)
        # Reassuring empty-state, drawn over the table viewport so rowCount() stays 0 (an overlay,
        # never an inserted row). Positioned + toggled in refresh().
        self._empty_label = QLabel(
            "Nothing keeps reappearing near you right now.", self._table.viewport())
        self._empty_label.setWordWrap(True)
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setStyleSheet(f"color:{C.TEXT_MUTED};")
        self._empty_label.hide()

        row = QHBoxLayout()
        self._ignore_btn = QPushButton("Mark selected as mine (ignore)")
        self._ignore_btn.setToolTip(
            "Add the selected device to your ignore list — your own phone/watch stay filtered.")
        self._ignore_btn.clicked.connect(self._mark_selected_ignored)
        row.addWidget(self._ignore_btn)
        row.addStretch(1)
        outer.addLayout(row)

    def showEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        super().showEvent(ev)
        self._timer.start()
        self.refresh()

    def hideEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        super().hideEvent(ev)
        self._timer.stop()

    def refresh(self) -> None:
        """Repaint the flagged-device table from tracker.tails(now, threshold). With a shared
        MetricsModel wired, also route the flagged tails to ALERT readings (Dashboard)."""
        now = self._now()
        hits = self._tracker.tails(now, self._threshold)
        if self._model is not None:
            tails_to_alerts(self._tracker, self._model, now, self._threshold)
        self._table.setRowCount(len(hits))
        for r, h in enumerate(hits):
            self._table.setItem(r, 0, QTableWidgetItem(h.device))
            self._table.setItem(r, 1, QTableWidgetItem(h.label))
            pers = QTableWidgetItem(f"{h.persistence:.2f}")
            pers.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if h.persistence >= 0.8:       # near-constant presence — the strongest tail signal
                pers.setForeground(QColor(C.ERROR))
            elif h.persistence >= 0.5:
                pers.setForeground(QColor(C.WARNING))
            self._table.setItem(r, 2, pers)
            win = QTableWidgetItem(str(h.windows))
            win.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(r, 3, win)
        n = len(hits)
        self._count_label.setText(
            "No devices flagged." if n == 0
            else f"{n} device{'' if n == 1 else 's'} keep reappearing")
        self._count_label.setStyleSheet(
            f"color:{C.TEXT_MUTED};" if n == 0 else f"color:{C.WARNING};")
        self._empty_label.setGeometry(self._table.viewport().rect())
        self._empty_label.setVisible(n == 0)

    def _on_threshold(self, value: float) -> None:
        self._threshold = float(value)
        self.refresh()

    def _selected_device(self) -> str:
        items = self._table.selectedItems()
        if not items:
            return ""
        cell = self._table.item(items[0].row(), 0)
        return cell.text() if cell is not None else ""

    def _mark_selected_ignored(self) -> None:
        dev = self._selected_device()
        if not dev:
            return
        self._tracker.add_ignore(dev)   # drop it + never score it again (your own device)
        self._save_ignore()
        self.refresh()

    # ── ignore-list persistence (Settings) ──
    def _load_ignore(self) -> "set[str]":
        try:
            from src.config.settings import load_settings
            return set((load_settings().get(_SETTINGS_KEY) or {}).get("ignore") or [])
        except Exception:  # noqa: BLE001 — a settings hiccup must not block the panel
            return set()

    def _save_ignore(self) -> None:
        try:
            from src.config.settings import load_settings, save_settings
            s = load_settings()
            s.setdefault(_SETTINGS_KEY, {})["ignore"] = sorted(self._tracker.ignore)
            save_settings(s)
        except Exception:  # noqa: BLE001 — persistence is best-effort
            pass

    def shutdown(self) -> None:
        """Stop the timer + detach the ingestor observer (the host calls this on teardown)."""
        try:
            self._timer.stop()
        except Exception:  # noqa: BLE001
            pass
        if self._observer is not None and self._ingestor is not None:
            try:
                self._ingestor.remove_event_observer(self._observer)
            except Exception:  # noqa: BLE001 — detach is best-effort
                pass
            self._observer = None
