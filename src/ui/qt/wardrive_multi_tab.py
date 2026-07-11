"""Multi-device wardrive tab — GPS-tagged capture across several boards at once (F1 slice 4b).

Thin Qt wrapper over :class:`~src.core.wardrive_multi.MultiWardriveController`: pick which connected boards
to drive, share one GPS + one merged WiGLE CSV, and watch per-board AP counts roll in. All the capture
logic lives in the (Qt-free, unit-tested) controller; this file is selection + a status table refreshed
on a timer. Non-trivial bits (`_refresh_boards`, `_checked_boards`, `_apply_snapshot`) are factored so they
test offscreen without a live controller.

LAWFUL, OWNER-AUTHORIZED USE ONLY — same passive beacon+GPS logging as the single-board Wardrive tab.
"""
from __future__ import annotations

import logging

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core.wardrive_multi import MultiWardriveController
from src.ui.qt.flash_tab import _make_card
from src.ui.qt.wardrive_tab import _list_serial_ports

log = logging.getLogger(__name__)

_STATUS_COLS = ("Port", "Firmware", "APs", "Started")


class WardriveMultiTab(QWidget):
    """Drive a GPS-tagged wardrive across many boards, aggregated into one WiGLE CSV."""

    def __init__(self, device_manager=None) -> None:
        super().__init__()
        self._dm = device_manager
        self._controller: MultiWardriveController | None = None
        self._fh = None
        self._seen_ports: set[str] = set()
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._build_ui()
        self._refresh_boards()
        self._refresh_ports()

    # ── UI ────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        _scroll = QScrollArea(self)
        _scroll.setWidgetResizable(True)
        _scroll.setFrameShape(QScrollArea.NoFrame)
        _content = QWidget()
        _scroll.setWidget(_content)
        _outer = QVBoxLayout(self)
        _outer.setContentsMargins(0, 0, 0, 0)
        _outer.addWidget(_scroll)
        root = QVBoxLayout(_content)
        banner = QLabel("⚠ Lawful, owner-authorized use only. Passive beacon + GPS logging across every "
                        "selected board, merged into one WiGLE CSV. No deauth, no payload capture.")
        banner.setWordWrap(True)
        banner.setStyleSheet("color:#d29922;")
        root.addWidget(banner)

        boards_card, boards_l = _make_card("Boards")
        row = QHBoxLayout()
        self._board_list = QListWidget()
        self._board_list.setSelectionMode(QAbstractItemView.NoSelection)
        row.addWidget(self._board_list, 1)
        btns = QVBoxLayout()
        self._btn_refresh = QPushButton("Refresh")
        self._btn_refresh.setToolTip("Re-read the connected boards from the Devices tab.")
        self._btn_refresh.clicked.connect(self._refresh_boards)
        btns.addWidget(self._btn_refresh)
        btns.addStretch(1)
        row.addLayout(btns)
        boards_l.addLayout(row)
        boards_l.addWidget(QLabel("Tick each board to include. Firmware comes from the Devices tab; a board "
                                  "with unknown firmware uses the Marauder default."))
        root.addWidget(boards_card)

        gps_card, gps_l = _make_card("GPS + output")
        g = QHBoxLayout()
        g.addWidget(QLabel("GPS port:"))
        self._gps_combo = QComboBox()
        g.addWidget(self._gps_combo, 1)
        g.addWidget(QLabel("Baud:"))
        self._gps_baud = QLineEdit("9600")
        self._gps_baud.setMaximumWidth(80)
        g.addWidget(self._gps_baud)
        g.addWidget(QLabel("Dev baud:"))
        self._dev_baud = QLineEdit("115200")
        self._dev_baud.setMaximumWidth(80)
        g.addWidget(self._dev_baud)
        gps_l.addLayout(g)
        o = QHBoxLayout()
        o.addWidget(QLabel("Output CSV:"))
        self._out_edit = QLineEdit("multi-wardrive.csv")
        o.addWidget(self._out_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_out)
        o.addWidget(browse)
        gps_l.addLayout(o)
        root.addWidget(gps_card)

        ctl = QHBoxLayout()
        self._btn_start = QPushButton("Start all")
        self._btn_start.clicked.connect(self._on_start)
        self._btn_stop = QPushButton("Stop all")
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop)
        ctl.addWidget(self._btn_start)
        ctl.addWidget(self._btn_stop)
        ctl.addStretch(1)
        self._total_label = QLabel("Fix: No Fix    Total APs: 0")
        ctl.addWidget(self._total_label)
        root.addLayout(ctl)

        self._status_table = QTableWidget(0, len(_STATUS_COLS))
        self._status_table.setHorizontalHeaderLabels(_STATUS_COLS)
        self._status_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._status_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        root.addWidget(self._status_table, 1)

    # ── board selection (testable) ───────────────────────────────────
    def _connected_boards(self) -> list[tuple[str, str]]:
        if self._dm is None:
            return []
        try:
            return [(getattr(d, "port", ""), (getattr(d, "firmware", "") or ""))
                    for d in self._dm.list_connected() if getattr(d, "port", "")]
        except Exception:  # noqa: BLE001
            return []

    def _refresh_boards(self) -> None:
        checked = {p for p, _ in self._checked_boards()}
        boards = self._connected_boards()
        self._board_list.clear()
        for port, fw in boards:
            item = QListWidgetItem(f"{port}  —  {fw or '(unknown fw → Marauder default)'}")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            # keep a board's tick across a refresh; default genuinely new (never-seen) boards to checked
            new_board = port not in self._seen_ports
            item.setCheckState(Qt.Checked if (port in checked or new_board) else Qt.Unchecked)
            item.setData(Qt.UserRole, (port, fw))
            self._board_list.addItem(item)
        self._seen_ports = {port for port, _ in boards}

    def _checked_boards(self) -> list[tuple[str, str]]:
        out = []
        for i in range(self._board_list.count()):
            item = self._board_list.item(i)
            if item.checkState() == Qt.Checked:
                data = item.data(Qt.UserRole)
                if data:
                    out.append((data[0], data[1]))
        return out

    def _refresh_ports(self) -> None:
        self._gps_combo.clear()
        self._gps_combo.addItem("(none)", None)
        for dev, desc in _list_serial_ports():
            self._gps_combo.addItem(f"{dev} — {desc}", dev)

    def _browse_out(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save WiGLE CSV", self._out_edit.text(), "CSV (*.csv)")
        if path:
            self._out_edit.setText(path)

    # ── status rendering (testable) ──────────────────────────────────
    def _apply_snapshot(self, snap: dict) -> None:
        boards = snap.get("boards", [])
        self._status_table.setRowCount(len(boards))
        for r, b in enumerate(boards):
            cells = (str(b.get("port", "")),
                     b.get("firmware", "") or "(auto)",
                     str(b.get("aps", 0)),
                     "yes" if b.get("started") else "no")
            for c, val in enumerate(cells):
                self._status_table.setItem(r, c, QTableWidgetItem(val))
        self._total_label.setText(f"Fix: {snap.get('fix', 'No Fix')}    Total APs: {snap.get('total_aps', 0)}")

    # ── lifecycle ─────────────────────────────────────────────────────
    def _on_start(self) -> None:
        if self._dm is None:
            self._total_label.setText("No device manager available.")
            return
        boards = self._checked_boards()
        if not boards:
            self._total_label.setText("Tick at least one board first.")
            return
        out = self._out_edit.text().strip()
        if not out:
            self._total_label.setText("Choose an output CSV path.")
            return
        try:
            dev_baud = int(self._dev_baud.text() or "115200")
            gps_baud = int(self._gps_baud.text() or "9600")
        except ValueError:
            self._total_label.setText("Baud must be a number.")
            return
        try:
            self._fh = open(out, "w", newline="", encoding="utf-8")
        except OSError as exc:
            self._total_label.setText(f"Cannot open {out}: {exc}")
            return
        gps = self._gps_combo.currentData() or ""
        self._controller = MultiWardriveController(self._dm, self._fh, gps_port=gps, gps_baud=gps_baud)
        for port, fw in boards:
            self._controller.add_board(port, baud=dev_baud, firmware=fw)
        self._controller.start()
        self._timer.start()
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)

    def _on_stop(self) -> None:
        self._timer.stop()
        if self._controller is not None:
            self._controller.stop()
            self._apply_snapshot(self._controller.snapshot())
            self._controller = None
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._fh = None
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)

    def _tick(self) -> None:
        if self._controller is not None:
            self._apply_snapshot(self._controller.snapshot())

    def shutdown(self) -> None:
        """Stop an in-progress multi-board capture on app teardown (see WardriveTab.shutdown).

        Without it, the firmware on every ticked board is left scanning (no STOP verb) and the shared WiGLE
        CSV isn't closed on exit. Called by the main window's closeEvent.
        """
        if self._btn_stop.isEnabled():   # a capture is active
            try:
                self._on_stop()          # controller.stop() (STOP verb per board) + CSV close
            except Exception:            # noqa: BLE001 — teardown must never raise
                pass
