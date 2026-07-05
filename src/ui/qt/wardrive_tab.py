"""Wardrive tab — GPS-tagged Wi-Fi capture exported as WiGLE CSV (cyber-controller only).

LAWFUL, OWNER-AUTHORIZED USE ONLY. Passive logging of broadcast beacon metadata + your own GPS
position (like WiGLE / Marauder wardrive mode). Reads NMEA from a GPS serial port and AP scan lines
from the ESP32 device serial port, and writes a deduped WiGLE CSV via :mod:`src.core.wardrive`.
The heavy lifting + the file format are tested in src/core/wardrive.py; this tab is the live glue.
"""

from __future__ import annotations

import logging
import os
import threading

from PyQt5.QtCore import QObject, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.core import wardrive as wd
from src.ui.qt.flash_tab import _make_card

log = logging.getLogger(__name__)


def _list_serial_ports() -> list[tuple[str, str]]:
    try:
        from serial.tools import list_ports
        return [(p.device, p.description or p.device) for p in list_ports.comports()]
    except Exception:  # noqa: BLE001
        return []


class _WardriveWorker(QThread):
    status = pyqtSignal(str, int)   # gps-fix text, ap count
    line = pyqtSignal(str)
    stopped = pyqtSignal()

    def __init__(self, gps_port: str, gps_baud: int, dev_port: str, dev_baud: int, out_path: str) -> None:
        super().__init__()
        self._gps_port, self._gps_baud = gps_port, gps_baud
        self._dev_port, self._dev_baud = dev_port, dev_baud
        self._out_path = out_path
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            import serial
        except Exception as exc:  # noqa: BLE001
            self.line.emit(f"pyserial unavailable: {exc}")
            self.stopped.emit()
            return
        try:
            fh = open(self._out_path, "w", newline="", encoding="utf-8")
        except OSError as exc:
            self.line.emit(f"cannot open output {self._out_path}: {exc}")
            self.stopped.emit()
            return

        sess = wd.WardriveSession(fh, app_version="1.0")
        sess.start()
        gps = dev = None
        last = ("", -1)
        try:
            if self._gps_port:
                gps = serial.Serial(self._gps_port, self._gps_baud, timeout=0.5)
            dev = serial.Serial(self._dev_port, self._dev_baud, timeout=0.5)
            try:
                dev.write(b"scanap\n")
            except Exception:  # noqa: BLE001
                pass
            self.line.emit(f"Wardrive started — logging to {self._out_path}")
            while not self._stop:
                if gps is not None:
                    try:
                        gl = gps.readline().decode("ascii", "replace").strip()
                        if gl:
                            sess.update_gps(gl)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    dl = dev.readline().decode("utf-8", "replace").strip()
                    if dl and sess.observe(dl):
                        self.line.emit(f"+ {dl[:80]}")
                except Exception:  # noqa: BLE001
                    pass
                fix = sess.fix
                ftxt = f"{fix.lat:.5f}, {fix.lon:.5f}" if (fix and fix.has_fix) else "No Fix"
                cur = (ftxt, sess.ap_count)
                if cur != last:
                    self.status.emit(ftxt, sess.ap_count)
                    last = cur
        except Exception as exc:  # noqa: BLE001
            self.line.emit(f"wardrive error: {exc}")
        finally:
            try:
                if dev is not None:
                    dev.write(b"stopscan\n")
                    dev.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                if gps is not None:
                    gps.close()
            except Exception:  # noqa: BLE001
                pass
            fh.close()
            self.line.emit(f"Wardrive stopped — {sess.ap_count} APs logged to {self._out_path}")
            self.stopped.emit()


class _WardriveCapture(QObject):
    """GPS-tagged capture routed through the shared DeviceManager instead of a raw serial handle (F1).

    ``_WardriveWorker`` opened its OWN ``serial.Serial()``, which on Windows (COM ports are exclusive)
    throws Access Denied the moment the same board is also open in the Devices tab. This borrows both the
    device and the GPS port from the DeviceManager with an owner tag, so a board already connected elsewhere
    is shared through ref-counting rather than fought over — the fix for a real double-open bug. The DM's
    per-port reader threads push lines in via ``on_line`` (so no QThread of our own); a lock guards the
    session because the two ports fire on different threads. Same signal interface as ``_WardriveWorker``.
    """

    status = pyqtSignal(str, int)   # gps-fix text, ap count
    line = pyqtSignal(str)
    stopped = pyqtSignal()

    OWNER = "wardrive"

    def __init__(self, device_manager, gps_port: str, gps_baud: int,
                 dev_port: str, dev_baud: int, out_path: str) -> None:
        super().__init__()
        self._dm = device_manager
        self._gps_port, self._gps_baud = gps_port, gps_baud
        self._dev_port, self._dev_baud = dev_port, dev_baud
        self._out_path = out_path
        self._lock = threading.Lock()
        self._sess: wd.WardriveSession | None = None
        self._fh = None
        self._dev_conn = None
        self._gps_conn = None
        self._running = False
        self._last_status: tuple[str, int] = ("", -1)

    def start(self) -> None:
        try:
            self._fh = open(self._out_path, "w", newline="", encoding="utf-8")
        except OSError as exc:
            self.line.emit(f"cannot open output {self._out_path}: {exc}")
            self.stopped.emit()
            return
        self._sess = wd.WardriveSession(self._fh, app_version="1.0")
        self._sess.start()
        self._running = True
        try:
            self._dev_conn = self._dm.open_connection(self._dev_port, self._dev_baud, owner=self.OWNER)
            self._dev_conn.on_line(self._on_dev_line)
            if self._gps_port:
                self._gps_conn = self._dm.open_connection(self._gps_port, self._gps_baud, owner=self.OWNER)
                self._gps_conn.on_line(self._on_gps_line)
            try:
                self._dev_conn.write("scanap\n")
            except Exception:  # noqa: BLE001
                pass
            self.line.emit(f"Wardrive started — logging to {self._out_path}")
        except Exception as exc:  # noqa: BLE001
            self.line.emit(f"wardrive start error: {exc}")
            self.stop()

    def stop(self) -> None:
        if not self._running and self._dev_conn is None and self._gps_conn is None:
            return
        self._running = False
        # Tear the ports down FIRST (this drains their reader threads) so no callback can still be writing
        # when we close the file below. remove_line_callback stops new lines; close_connection releases our
        # owner ref (the board stays alive for any other owner).
        if self._dev_conn is not None:
            for step in (lambda: self._dev_conn.write("stopscan\n"),
                         lambda: self._dev_conn.remove_line_callback(self._on_dev_line),
                         lambda: self._dm.close_connection(self._dev_port, owner=self.OWNER)):
                try:
                    step()
                except Exception:  # noqa: BLE001
                    pass
            self._dev_conn = None
        if self._gps_conn is not None:
            for step in (lambda: self._gps_conn.remove_line_callback(self._on_gps_line),
                         lambda: self._dm.close_connection(self._gps_port, owner=self.OWNER)):
                try:
                    step()
                except Exception:  # noqa: BLE001
                    pass
            self._gps_conn = None
        with self._lock:                       # readers are released; safe to finalize the file
            count = self._sess.ap_count if self._sess else 0
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:  # noqa: BLE001
                    pass
                self._fh = None
        self.line.emit(f"Wardrive stopped — {count} APs logged to {self._out_path}")
        self.stopped.emit()

    # -- line callbacks (fire on the DeviceManager reader threads) --
    def _on_gps_line(self, ln: str) -> None:
        if not ln:
            return
        with self._lock:
            if not self._running or self._sess is None or self._fh is None:
                return
            self._sess.update_gps(ln)
        self._emit_status()

    def _on_dev_line(self, ln: str) -> None:
        if not ln:
            return
        with self._lock:
            if not self._running or self._sess is None or self._fh is None:
                return
            wrote = self._sess.observe(ln)
        if wrote:
            self.line.emit(f"+ {ln[:80]}")
        self._emit_status()

    def _emit_status(self) -> None:
        sess = self._sess
        if sess is None:
            return
        fix = sess.fix
        ftxt = f"{fix.lat:.5f}, {fix.lon:.5f}" if (fix and fix.has_fix) else "No Fix"
        cur = (ftxt, sess.ap_count)
        if cur != self._last_status:
            self._last_status = cur
            self.status.emit(ftxt, sess.ap_count)


class WardriveTab(QWidget):
    """Live GPS-tagged Wi-Fi capture -> WiGLE CSV."""

    def __init__(self, device_manager=None) -> None:
        super().__init__()
        self._dm = device_manager                       # when present, capture routes through it (no COM clash)
        self._worker = None
        self._build_ui()
        self._refresh_ports()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        banner = QLabel("⚠ Lawful, owner-authorized use only. This passively logs broadcast Wi-Fi "
                        "beacon metadata + your GPS position (like WiGLE). It does not deauth or capture "
                        "traffic. You are responsible for complying with local law.")
        banner.setWordWrap(True)
        banner.setStyleSheet("color:#f0883e;")
        root.addWidget(banner)

        port_card, port_layout = _make_card("Serial ports")
        row = QHBoxLayout()
        row.addWidget(QLabel("ESP32 (Marauder):"))
        self._dev_combo = QComboBox()
        self._dev_combo.setToolTip("Serial port of the ESP32 running Marauder (the AP scanner).")
        row.addWidget(self._dev_combo, 1)
        self._dev_baud = QLineEdit("115200")
        self._dev_baud.setToolTip("Marauder serial baud (default 115200).")
        self._dev_baud.setFixedWidth(80)
        row.addWidget(self._dev_baud)
        port_layout.addLayout(row)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("GPS (NMEA):"))
        self._gps_combo = QComboBox()
        self._gps_combo.setToolTip("Serial port of the GPS module (NMEA). Leave as '(none)' to log "
                                   "without coordinates — note WiGLE rows are only written with a fix.")
        row2.addWidget(self._gps_combo, 1)
        self._gps_baud = QLineEdit("9600")
        self._gps_baud.setToolTip("GPS serial baud (default 9600).")
        self._gps_baud.setFixedWidth(80)
        row2.addWidget(self._gps_baud)
        port_layout.addLayout(row2)
        btn_refresh = QPushButton("Refresh ports")
        btn_refresh.clicked.connect(self._refresh_ports)
        port_layout.addWidget(btn_refresh)
        root.addWidget(port_card)

        self._out_card, out_layout = _make_card("Output (WiGLE CSV)")
        out_card = self._out_card
        orow = QHBoxLayout()
        self._out_edit = QLineEdit(os.path.join(os.path.expanduser("~"), "wardrive-wigle.csv"))
        self._out_edit.setToolTip("WiGLE CSV file to write (WigleWifi-1.6). Upload it at wigle.net.")
        orow.addWidget(self._out_edit, 1)
        btn_out = QPushButton("Browse…")
        btn_out.clicked.connect(self._browse_out)
        orow.addWidget(btn_out)
        out_layout.addLayout(orow)
        root.addWidget(out_card)

        ctrl = QHBoxLayout()
        self._btn_start = QPushButton("Start wardrive")
        self._btn_start.setToolTip("Begin scanning + GPS-tagging APs to the WiGLE CSV.")
        self._btn_start.clicked.connect(self._on_start)
        ctrl.addWidget(self._btn_start)
        self._btn_stop = QPushButton("Stop")
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop)
        ctrl.addWidget(self._btn_stop)
        root.addLayout(ctrl)

        self._status = QLabel("GPS: —    APs logged: 0")
        root.addWidget(self._status)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMinimumHeight(140)
        root.addWidget(self._log, 1)

    # ── Dual-depth (Simple / Pro) ────────────────────────────────────

    def set_ui_mode(self, mode: str) -> None:
        """Simple = pick the ESP32 + GPS ports and Start/Stop, logging to the default WiGLE CSV. Hide
        the baud overrides and the output-path card (defaults to ~/wardrive-wigle.csv). Pro restores
        full control. Export format stays WiGLE CSV (plaintext, meant to be shared) in both modes."""
        pro = str(mode).lower() != "simple"
        for w in (getattr(self, "_dev_baud", None), getattr(self, "_gps_baud", None),
                  getattr(self, "_out_card", None)):
            if w is not None:
                w.setVisible(pro)

    def _refresh_ports(self) -> None:
        ports = _list_serial_ports()
        self._dev_combo.clear()
        for dev, desc in ports:
            self._dev_combo.addItem(f"{dev} — {desc}", dev)
        if self._dev_combo.count() == 0:
            self._dev_combo.addItem("(no serial ports)", None)
        self._gps_combo.clear()
        self._gps_combo.addItem("(none)", None)
        for dev, desc in ports:
            self._gps_combo.addItem(f"{dev} — {desc}", dev)

    def _browse_out(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save WiGLE CSV", self._out_edit.text(),
                                              "CSV (*.csv);;All files (*)")
        if path:
            self._out_edit.setText(path)

    def _on_start(self) -> None:
        dev = self._dev_combo.currentData()
        if not dev:
            self._logmsg("No ESP32 serial port selected.")
            return
        try:
            dbaud = int(self._dev_baud.text() or "115200")
            gbaud = int(self._gps_baud.text() or "9600")
        except ValueError:
            self._logmsg("Baud must be a number.")
            return
        out = self._out_edit.text().strip()
        if not out:
            self._logmsg("Choose an output CSV path.")
            return
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        gps = self._gps_combo.currentData()
        if self._dm is not None:
            # Route through the shared DeviceManager so a board also open in the Devices tab is shared,
            # not double-opened (Windows COM ports are exclusive — the raw-serial path throws Access Denied).
            self._worker = _WardriveCapture(self._dm, gps, gbaud, dev, dbaud, out)
        else:
            self._worker = _WardriveWorker(gps, gbaud, dev, dbaud, out)
        self._worker.status.connect(self._on_status)
        self._worker.line.connect(self._logmsg)
        self._worker.stopped.connect(self._on_stopped)
        self._worker.start()

    def _on_stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._btn_stop.setEnabled(False)

    def _on_status(self, fix_text: str, ap_count: int) -> None:
        self._status.setText(f"GPS: {fix_text}    APs logged: {ap_count}")

    def _on_stopped(self) -> None:
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)

    def _logmsg(self, msg: str) -> None:
        self._log.appendPlainText(msg)
        log.info("WardriveTab: %s", msg)
