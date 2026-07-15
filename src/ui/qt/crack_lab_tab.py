"""Crack Lab tab (formerly "Wi-Fi Audit") — the offline WPA key-recovery pipeline UI.

The engines (``crack_pipeline`` + ``wordlist_manager``) are finished + unit-tested, but before 1.7.0
they had NO reachable UI. This tab wires them honestly:

  pick a capture you made  ->  pick a wordlist you provide  ->  a per-run CONSENT affirmation
  ->  convert (hcxpcapngtool) + crack (your hashcat OR aircrack-ng)  ->  streamed log + result.

It is dictionary-only (no brute force), bundles/installs no cracking tools, and gates every run
behind :func:`crack_pipeline.consent_prompt_text`. The heavy subprocess work runs in a
``_CrackWorker(QThread)`` so the GUI never blocks; the worker streams tool output back via a signal.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path

from PyQt5.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core import crack_pipeline as cp
from src.core import tool_installer as ti
from src.core import wordlist_manager as wm
from src.core.capture_export import (
    CAPTURE_CSV_COLUMNS,
    export_captures_csv,
    export_captures_json,
)

log = logging.getLogger(__name__)


class _CrackWorker(QThread):
    """Runs the convert+crack pipeline off the GUI thread, streaming tool lines as they arrive."""

    line = pyqtSignal(str)
    done = pyqtSignal(object)      # emits a crack_pipeline.CrackResult

    def __init__(self, capture: str, wordlist: str, backend: str, bssid: str = "") -> None:
        super().__init__()
        self._capture = capture
        self._wordlist = wordlist
        self._backend = backend
        self._bssid = bssid
        self._stop = False
        # The aircrack/hashcat child process, once it spawns. request_stop() (GUI thread) and
        # _register_proc() (worker thread) both touch it, so guard it with a lock.
        self._proc = None
        self._proc_lock = threading.Lock()

    def _register_proc(self, proc) -> None:
        """Called on the worker thread the instant a crack child spawns. If Stop already fired, kill it
        immediately; otherwise stash it so request_stop() (GUI thread) can."""
        with self._proc_lock:
            self._proc = proc
            if self._stop:
                cp.kill_proc_tree(proc)

    def request_stop(self) -> None:
        """Cancel the run. The native engine polls ``self._stop`` between candidate batches (a clean exit
        without QThread.terminate(), which mid pure-Python loop can deadlock the GUI on the GIL). The
        aircrack/hashcat backends block in a subprocess with no cooperative poll, so ALSO kill the child
        process here — QThread.terminate() would only kill this wrapper thread and orphan the running
        aircrack/hashcat OS process (and skip the temp-file cleanup)."""
        self._stop = True
        with self._proc_lock:
            if self._proc is not None:
                cp.kill_proc_tree(self._proc)

    def run(self) -> None:  # noqa: D401 - QThread entry point
        emit = self.line.emit
        tmp_hash = None
        try:
            tools = cp.detect_tools()
            if self._backend == "native":
                # CC's own pure-Python cracker — no external tool, always available. Pass the
                # cooperative stop hook so Stop cancels without killing the thread.
                result = cp.run_native(self._capture, self._wordlist, emit, bssid=self._bssid,
                                       should_stop=lambda: self._stop)
            elif self._backend == "aircrack":
                result = cp.run_aircrack(self._capture, self._wordlist, emit,
                                         tools=tools, bssid=self._bssid, on_proc=self._register_proc)
            else:
                # hashcat path: convert the capture to .hc22000 first (unless already one)
                result = None
                if os.path.splitext(self._capture)[1].lower() == ".hc22000":
                    hash_file = self._capture
                else:
                    fd, hash_file = tempfile.mkstemp(suffix=".hc22000", prefix="cc_wifi_")
                    os.close(fd)
                    tmp_hash = hash_file  # ours to clean up (never the user's own .hc22000)
                    # Pass on_proc so Stop can kill hcxpcapngtool mid-convert (a large capture is slow).
                    n = cp.convert_capture(self._capture, hash_file, emit, tools=tools,
                                           on_proc=self._register_proc)
                    emit(f"[convert] {n} crackable hash(es) extracted.")
                    if n == 0:
                        # No PMKID/handshake in this capture — the honest negative. Feeding an empty
                        # .hc22000 to hashcat would exit nonzero and mis-report a "tool failure".
                        result = cp.CrackResult(
                            cracked=False, detail="no PMKID or handshake found in this capture")
                if result is None:
                    result = cp.run_hashcat(hash_file, self._wordlist, emit, tools=tools,
                                            on_proc=self._register_proc)
        except Exception as exc:  # never let a worker exception kill the thread silently
            log.exception("wifi-audit crack worker failed")
            result = cp.CrackResult(detail=f"error: {exc}")
        finally:
            if tmp_hash is not None:  # delete the temp .hc22000 we created (leak-free on any exit)
                try:
                    os.remove(tmp_hash)
                except OSError:
                    pass
        if self._stop and not result.cracked:
            # The user cancelled: the killed child's nonzero exit would otherwise read as a tool error,
            # so report an honest "stopped" instead. But a crack that ALREADY succeeded (the key verified
            # in the race window between the crack returning and this stop check) must NOT be thrown away
            # — a recovered key survives a late Stop.
            result = cp.CrackResult(detail="stopped")
        self.done.emit(result)


class _WordlistInstallWorker(QThread):
    """Downloads one catalog wordlist off the GUI thread, streaming progress lines."""

    line = pyqtSignal(str)
    done = pyqtSignal(str, str)  # (installed_path or "", error or "")

    def __init__(self, spec) -> None:
        super().__init__()
        self._spec = spec

    def run(self) -> None:  # noqa: D401 - QThread entry point
        try:
            path = wm.download_wordlist(self._spec, on_line=self.line.emit)
            self.done.emit(path, "")
        except Exception as exc:  # never let the worker die silently
            log.exception("wordlist install failed")
            self.done.emit("", str(exc))


class _WordlistCatalogDialog(QDialog):
    """Install a prepackaged wordlist from the curated catalog (download + integrity-verify), or see
    what's already installed. Downloads run off the GUI thread and stream into the terminal."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Wordlist catalog")
        self.setMinimumWidth(560)
        self._worker: _WordlistInstallWorker | None = None
        root = QVBoxLayout(self)

        info = QLabel(wm.install_choices_text())
        info.setWordWrap(True)
        root.addWidget(info)

        self._buttons: dict[str, QPushButton] = {}
        for spec in wm.catalog():
            row = QHBoxLayout()
            pin = "SHA-256 pinned" if wm.is_pinned(spec) else "size-only — verify manually"
            lbl = QLabel(
                f"<b>{spec.name}</b> — {wm.format_size(spec.size_bytes)} [{spec.category}] "
                f"· {pin}<br><span style='color:#8b949e;'>{spec.description}</span>")
            lbl.setWordWrap(True)
            row.addWidget(lbl, 1)
            installed = wm.is_installed(spec)
            btn = QPushButton("Installed ✓" if installed else "Install")
            btn.setEnabled(not installed)
            btn.clicked.connect(lambda _=False, s=spec: self._install(s))
            self._buttons[spec.id] = btn
            row.addWidget(btn)
            root.addLayout(row)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(120)
        self._log.setPlaceholderText("download progress appears here (and in the terminal)")
        root.addWidget(self._log)

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        root.addWidget(close)

    def _install(self, spec) -> None:
        if self._worker is not None and self._worker.isRunning():
            return  # one download at a time
        btn = self._buttons.get(spec.id)
        if btn is not None:
            btn.setEnabled(False)
            btn.setText("Installing…")
        self._worker = _WordlistInstallWorker(spec)
        self._worker.line.connect(self._on_line)
        self._worker.done.connect(lambda path, err, s=spec: self._on_done(s, path, err))
        self._worker.start()

    def _on_line(self, text: str) -> None:
        self._log.appendPlainText(text)
        try:
            from src.core.activity_log import activity_log
            activity_log().emit_line("install", text)
        except Exception:  # noqa: BLE001
            pass

    def _on_done(self, spec, path: str, err: str) -> None:
        btn = self._buttons.get(spec.id)
        if err:
            self._log.appendPlainText(f"[error] {err}")
            if btn is not None:
                btn.setEnabled(True)
                btn.setText("Retry")
        else:
            self._log.appendPlainText(f"[ok] installed {path}")
            if btn is not None:
                btn.setText("Installed ✓")
                btn.setEnabled(False)

    def closeEvent(self, event) -> None:
        self._join_worker()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        # The "Close" button (accept) and Escape (reject) route through done(), NOT closeEvent — so
        # join the worker here too, else a running download keeps going detached after the dialog is
        # dismissed (and risks a QThread-destroyed-while-running abort on teardown).
        self._join_worker()
        super().done(result)

    def _join_worker(self) -> None:
        w = self._worker
        if w is not None and w.isRunning():
            w.terminate()   # like the crack worker's stop; a killed run leaves only a .part temp
            w.wait(2000)


class _ToolEnableWorker(QThread):
    """Unpacks one bundled tool pack off the GUI thread (after the folder is Defender-excluded)."""

    line = pyqtSignal(str)
    done = pyqtSignal(bool, str)  # (ok, message)

    def __init__(self, pack) -> None:
        super().__init__()
        self._pack = pack

    def run(self) -> None:  # noqa: D401 - QThread entry point
        try:
            from src.core import tool_bundle
            ok, msg = tool_bundle.enable_bundled(self._pack, on_line=self.line.emit)
            self.done.emit(ok, msg)
        except Exception as exc:  # never let the worker die silently
            log.exception("enable bundled tool failed")
            self.done.emit(False, str(exc))


class _ToolsDialog(QDialog):
    """OPTIONAL external engines. Crack Lab's built-in native cracker already works; aircrack-ng/hashcat
    are extras (faster + full CLI). They ship as encrypted packs; enabling one unpacks it — but Windows
    Defender flags them as PUA, so it's gated behind a one-time, opt-in folder exclusion the user
    controls. There is NO fetching (the vendor host is unreliable) — everything is bundled."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Optional external engines")
        self.setMinimumWidth(660)
        self._worker: _ToolEnableWorker | None = None
        from src.core import defender, tool_bundle
        self._defender = defender
        self._tool_bundle = tool_bundle
        root = QVBoxLayout(self)

        intro = QLabel(
            "Crack Lab's <b>built-in native cracker works right now</b> — no install, no antivirus "
            "prompts. These external tools are OPTIONAL: aircrack-ng and hashcat can be faster (hashcat "
            "uses your GPU) and give you their full command line in the terminal.")
        intro.setWordWrap(True)
        root.addWidget(intro)

        excl_dir = tool_bundle.enable_dir()
        if defender.is_windows() and defender.pua_protection_on() is not False:
            notice = QLabel(
                "<b>Heads-up:</b> Windows Defender flags these standard tools as PUA and will delete "
                "them unless you add a one-time exclusion for CC's tools folder. That turns off Defender "
                "scanning for THAT folder only — a deliberate trade-off you opt into. Prefer not to? The "
                "native cracker already does the job.")
            notice.setWordWrap(True)
            notice.setObjectName("muted")
            root.addWidget(notice)
            cmd_row = QHBoxLayout()
            self._cmd_edit = QLineEdit(defender.exclusion_command(excl_dir))
            self._cmd_edit.setReadOnly(True)
            cmd_row.addWidget(self._cmd_edit, 1)
            copy_btn = QPushButton("Copy")
            copy_btn.clicked.connect(self._copy_cmd)
            cmd_row.addWidget(copy_btn)
            uac_btn = QPushButton("Add exclusion (admin)")
            uac_btn.clicked.connect(self._add_exclusion)
            cmd_row.addWidget(uac_btn)
            root.addLayout(cmd_row)
            hint = QLabel("Run that in an <b>admin PowerShell</b>, or click “Add exclusion (admin)” for a "
                          "UAC prompt — then Enable a tool below.")
            hint.setWordWrap(True)
            hint.setObjectName("muted")
            root.addWidget(hint)

        self._enable_buttons: dict[str, QPushButton] = {}
        for pack in tool_bundle.list_packs():
            row = QHBoxLayout()
            lbl = QLabel(f"<b>{pack.tool}</b> {pack.version} — bundled (encrypted). Enable to unpack it "
                         "and use its full CLI from the terminal.")
            lbl.setWordWrap(True)
            row.addWidget(lbl, 1)
            btn = QPushButton("Enable…")
            btn.clicked.connect(lambda _=False, p=pack: self._enable(p))
            self._enable_buttons[pack.name] = btn
            row.addWidget(btn)
            root.addLayout(row)

        for r in ti.tool_availability():
            if r.present:
                root.addWidget(QLabel(f"✓ {r.tool} already detected ({r.source}) — usable as-is."))

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(120)
        self._log.setPlaceholderText("progress appears here (and in the terminal)")
        root.addWidget(self._log)

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        root.addWidget(close)

    def _copy_cmd(self) -> None:
        from PyQt5.QtWidgets import QApplication
        QApplication.clipboard().setText(self._cmd_edit.text())
        self._log.appendPlainText("[tools] exclusion command copied to clipboard")

    def _add_exclusion(self) -> None:
        self._log.appendPlainText("[tools] requesting an elevated Defender exclusion — approve the UAC "
                                  "prompt…")
        ok = self._defender.add_exclusion_elevated(self._tool_bundle.enable_dir())
        self._log.appendPlainText("[tools] exclusion added." if ok else
                                  "[tools] not added (declined/failed) — run the command manually.")

    def _enable(self, pack) -> None:
        if self._worker is not None and self._worker.isRunning():
            return  # one at a time
        btn = self._enable_buttons.get(pack.name)
        if btn is not None:
            btn.setEnabled(False)
            btn.setText("Enabling…")
        self._worker = _ToolEnableWorker(pack)
        self._worker.line.connect(self._on_line)
        self._worker.done.connect(lambda ok, msg, p=pack: self._on_done(p, ok, msg))
        self._worker.start()

    def _on_line(self, text: str) -> None:
        self._log.appendPlainText(text)
        try:
            from src.core.activity_log import activity_log
            activity_log().emit_line("tools", text)
        except Exception:  # noqa: BLE001
            pass

    def _on_done(self, pack, ok: bool, msg: str) -> None:
        self._log.appendPlainText(("[ok] " if ok else "[error] ") + msg)
        btn = self._enable_buttons.get(pack.name)
        if btn is not None:
            btn.setText("Enabled ✓" if ok else "Retry")
            btn.setEnabled(not ok)

    def closeEvent(self, event) -> None:
        self._join_worker()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        # "Close" (accept) + Escape (reject) bypass closeEvent — join the enable worker here too so a
        # running unpack doesn't continue detached after the dialog is gone.
        self._join_worker()
        super().done(result)

    def _join_worker(self) -> None:
        w = self._worker
        if w is not None and w.isRunning():
            w.terminate()
            w.wait(2000)


class _CaptureBridge(QObject):
    """Marshals CaptureStore bus callbacks (any thread) onto the Qt GUI thread."""

    changed = pyqtSignal()


class CrackLabTab(QWidget):
    """Reachable UI for the offline WPA dictionary attack (capture -> wordlist -> crack).

    Constructor takes the optional cross-comm *hub*; when present, the Captures table auto-populates
    from the shared :class:`~src.core.capture_store.CaptureStore` (a row appears when a device
    captures a handshake) and a solved crack writes back onto its record. With ``hub=None`` the tab
    degrades to manual-only (empty Captures table, no crash) — graceful-degrade like the hub.
    """

    def __init__(self, hub=None) -> None:
        super().__init__()
        self._worker: _CrackWorker | None = None
        self._backends_cache: list[str] = []   # detected crack backends; refreshed by _refresh_tools()

        # Shared capture log (auto-populates the Captures table). None when the hub is unavailable.
        self._captures = getattr(hub, "captures", None) if hub is not None else None
        self._cap_row_keys: list[str] = []      # table row index -> CaptureRecord.key
        self._active_capture_key = ""      # record a double-click loaded (for crack write-back)
        self._cap_bridge = _CaptureBridge()
        self._cap_bridge.changed.connect(self._refresh_captures, Qt.QueuedConnection)
        # Scroll-wrap the content so the engine/capture/wordlist rows never clip on a small/deck window
        # (setWidgetResizable lets the log still expand to fill when there's room).
        _scroll = QScrollArea(self)
        _scroll.setWidgetResizable(True)
        _scroll.setFrameShape(QScrollArea.NoFrame)
        _content = QWidget()
        _scroll.setWidget(_content)
        _outer = QVBoxLayout(self)
        _outer.setContentsMargins(0, 0, 0, 0)
        _outer.addWidget(_scroll)
        root = QVBoxLayout(_content)

        info = QLabel(cp.capability_text())
        info.setWordWrap(True)
        root.addWidget(info)

        # tools presence
        tools_box = QGroupBox("Engine (built-in native cracker always works — optional faster engines below)")
        tl = QHBoxLayout(tools_box)
        self._tools_label = QLabel("…")
        self._tools_label.setWordWrap(True)
        tl.addWidget(self._tools_label, 1)
        get_tools = QPushButton("Get tools…")
        get_tools.clicked.connect(self._show_tools)
        tl.addWidget(get_tools)
        recheck = QPushButton("Re-check")
        recheck.clicked.connect(self._refresh_tools)
        tl.addWidget(recheck)
        root.addWidget(tools_box)

        # capture picker
        cap_row = QHBoxLayout()
        cap_row.addWidget(QLabel("Capture:"))
        self._capture_edit = QLineEdit()
        self._capture_edit.setPlaceholderText("a .pcapng/.pcap/.cap/.hc22000 file you captured")
        # Typing a new path by hand breaks the double-click binding, so a solved crack won't get
        # written back onto a capture the user is no longer cracking (textEdited fires on user edits
        # only, NOT on the programmatic setText a row double-click does).
        self._capture_edit.textEdited.connect(self._forget_active_capture)
        cap_row.addWidget(self._capture_edit, 1)
        browse_cap = QPushButton("Browse…")
        browse_cap.clicked.connect(self._pick_capture)
        cap_row.addWidget(browse_cap)
        root.addLayout(cap_row)

        # ── Captures (auto-populated from live device captures) ──────────
        cap_box = QGroupBox("Captured handshakes")
        cb = QVBoxLayout(cap_box)
        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("Auto-logged as devices capture; double-click a row to load it."))
        hdr.addStretch(1)
        self._export_captures_btn = QPushButton("Export…")
        self._export_captures_btn.setToolTip(
            "Write the capture log to a spreadsheet-safe CSV or a JSON file — pick the format "
            "in the save dialog (includes any recovered passwords).")
        self._export_captures_btn.clicked.connect(self._on_export_captures)
        hdr.addWidget(self._export_captures_btn)
        cb.addLayout(hdr)
        self._captures_table = QTableWidget(0, len(CAPTURE_CSV_COLUMNS))
        self._captures_table.setHorizontalHeaderLabels([c.upper() for c in CAPTURE_CSV_COLUMNS])
        self._captures_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._captures_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._captures_table.cellDoubleClicked.connect(self._on_capture_activated)
        cb.addWidget(self._captures_table)
        root.addWidget(cap_box)

        # wordlist picker
        wl_row = QHBoxLayout()
        wl_row.addWidget(QLabel("Wordlist:"))
        self._wordlist_combo = QComboBox()
        self._wordlist_combo.setEditable(False)
        wl_row.addWidget(self._wordlist_combo, 1)
        byo = QPushButton("BYO…")
        byo.clicked.connect(self._pick_byo_wordlist)
        wl_row.addWidget(byo)
        refresh_wl = QPushButton("Refresh")
        refresh_wl.clicked.connect(self._refresh_wordlists)
        wl_row.addWidget(refresh_wl)
        catalog = QPushButton("Catalog…")
        catalog.clicked.connect(self._show_catalog)
        wl_row.addWidget(catalog)
        root.addLayout(wl_row)

        # backend + optional BSSID
        opt_row = QHBoxLayout()
        opt_row.addWidget(QLabel("Engine:"))
        self._backend_combo = QComboBox()
        opt_row.addWidget(self._backend_combo)
        opt_row.addWidget(QLabel("BSSID (aircrack, optional):"))
        self._bssid_edit = QLineEdit()
        self._bssid_edit.setPlaceholderText("AA:BB:CC:DD:EE:FF")
        opt_row.addWidget(self._bssid_edit, 1)
        root.addLayout(opt_row)

        # run / stop
        run_row = QHBoxLayout()
        self._run_btn = QPushButton("Recover key…")
        self._run_btn.clicked.connect(self._on_run)
        run_row.addWidget(self._run_btn)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        run_row.addWidget(self._stop_btn)
        run_row.addStretch(1)
        self._result_label = QLabel("")
        self._result_label.setWordWrap(True)
        run_row.addWidget(self._result_label, 1)
        root.addLayout(run_row)

        # log
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setPlaceholderText("tool output appears here during a run")
        root.addWidget(self._log, 1)

        self._refresh_tools()
        self._refresh_wordlists()

        # Subscribe to the shared capture log and paint any captures already present. Bus callbacks
        # arrive on the ingest thread, so they only poke the bridge signal (queued to GUI thread).
        if self._captures is not None:
            for topic in ("capture.added", "capture.updated", "capture.removed",
                          "capture.cleared", "capture.cracked"):
                self._captures.bus.subscribe(topic, self._on_capture_event)
            self._refresh_captures()

    # ── captures ─────────────────────────────────────────────────────
    def _on_capture_event(self, _topic: str, _payload) -> None:
        """CaptureStore bus callback (any thread) — request a GUI-thread table refresh."""
        self._cap_bridge.changed.emit()

    def _refresh_captures(self) -> None:
        """Rebuild the Captures table from the shared store snapshot (GUI thread only)."""
        if self._captures is None:
            return
        caps = self._captures.all()
        self._cap_row_keys = [c.key for c in caps]
        self._captures_table.setRowCount(len(caps))
        for row, c in enumerate(caps):
            d = c.to_dict()
            for col, name in enumerate(CAPTURE_CSV_COLUMNS):
                val = d.get(name)
                item = QTableWidgetItem("" if val is None else str(val))
                if c.crack_status == "cracked":
                    item.setForeground(Qt.darkGreen)   # a solved capture reads green across its row
                self._captures_table.setItem(row, col, item)

    def _on_capture_activated(self, row: int, _col: int) -> None:
        """Double-click a capture row -> load its file into the cracker (reuses the run flow)."""
        if self._captures is None or not (0 <= row < len(self._cap_row_keys)):
            return
        rec = self._captures.get(self._cap_row_keys[row])
        if rec is None:
            return
        path = rec.pcap_path or rec.hc22000_path
        if path:
            self._capture_edit.setText(path)
            self._active_capture_key = rec.key
            self._bssid_edit.setText(rec.bssid)   # helps the aircrack backend target this AP
        elif rec.pmkid:
            # Inline ESP32-DIV PMKID (no file on disk yet). Staging it to a .hc22000 is a separate
            # slice; be honest rather than write a possibly-malformed hashline.
            QMessageBox.information(
                self, "Inline PMKID",
                "This capture is an inline PMKID with no saved file yet. Save/convert it to a "
                ".hc22000 first, then Browse… to load it.")
        else:
            QMessageBox.information(
                self, "No file", "This capture has no saved .pcap/.hc22000 file to crack yet.")

    def _on_export_captures(self) -> None:
        """Write the capture log to a CSV or JSON file the operator picks (synchronous, fast write).

        One button, two formats (as the changelog promises): the save dialog offers a CSV and a
        JSON filter. The path extension decides the format when present, else the chosen filter
        does -- and the matching extension is appended so the file is never format-ambiguous."""
        if self._captures is None or not self._captures.all():
            QMessageBox.information(self, "Export", "No captures to export yet.")
            return
        default = str(Path.home() / "cyber-controller-captures.csv")
        path, selected = QFileDialog.getSaveFileName(
            self, "Export captures", default, "CSV (*.csv);;JSON (*.json);;All files (*)")
        if not path:
            return
        low = path.lower()
        if low.endswith(".json"):
            fmt = "json"
        elif low.endswith(".csv"):
            fmt = "csv"
        else:
            # No/unknown extension (e.g. the "All files" filter) -- let the chosen filter decide, then
            # give the file its extension so CSV and JSON exports stay distinguishable on disk.
            fmt = "json" if "json" in selected.lower() else "csv"
            path += f".{fmt}"
        exporter = export_captures_json if fmt == "json" else export_captures_csv
        try:
            n = exporter(self._captures.all(), path)
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        QMessageBox.information(self, "Export", f"Wrote {n} capture(s) to {path}")

    # ── populate ─────────────────────────────────────────────────────
    def _refresh_tools(self) -> None:
        tools = cp.detect_tools()
        parts = []
        for st in tools.values():
            mark = "✓" if st.present else "✗"
            parts.append(f"{mark} {st.name}" + (f" ({st.version})" if st.version else ""))
        self._tools_label.setText("   ".join(parts) or "no tools detected")
        backends = cp.available_backends(tools)
        # Cache the detected backends. detect_tools() spawns a --version subprocess per external tool, so
        # re-probing it on the GUI thread after every run (in _reset_run_buttons) hitched the event loop.
        # Refresh the cache here — where the tool set genuinely changes (startup, tool install/recheck).
        self._backends_cache = backends
        self._backend_combo.clear()
        self._backend_combo.addItems(backends or ["(install hashcat or aircrack-ng)"])
        self._run_btn.setEnabled(bool(backends))

    def _refresh_wordlists(self) -> None:
        current = self._wordlist_combo.currentData()
        self._wordlist_combo.clear()
        # Merge the bundled tiny WPA core (offline) with the user's installed/BYO lists.
        # Dedup by filename, preferring a user-installed copy over the bundled one (same content).
        seen: set[str] = set()
        for entry in wm.scan_installed():
            self._wordlist_combo.addItem(f"{entry['name']}  ({entry['size_human']})", entry["path"])
            seen.add(entry["name"])
        for entry in wm.bundled_wordlists():
            if entry["name"] in seen:
                continue
            self._wordlist_combo.addItem(
                f"{entry['name']}  ({entry['size_human']})  · bundled", entry["path"])
        if self._wordlist_combo.count() == 0:
            self._wordlist_combo.addItem("(no wordlists — use BYO… or Catalog…)", "")
        if current:
            idx = self._wordlist_combo.findData(current)
            if idx >= 0:
                self._wordlist_combo.setCurrentIndex(idx)

    # ── pickers ──────────────────────────────────────────────────────
    def _pick_capture(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a Wi-Fi capture", "",
            "Captures (*.pcapng *.pcap *.cap *.hc22000);;All files (*)")
        if path:
            self._capture_edit.setText(path)
            self._forget_active_capture()   # a browsed file is not the double-clicked record

    def _forget_active_capture(self, *_args) -> None:
        """Drop the capture<->crack write-back binding (the loaded file no longer maps to a row)."""
        self._active_capture_key = ""

    def _pick_byo_wordlist(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a wordlist", "", "Text (*.txt);;All files (*)")
        if not path:
            return
        try:
            wm.register_byo(path)
        except Exception as exc:
            QMessageBox.warning(self, "Wordlist", str(exc))
            return
        self._wordlist_combo.addItem(os.path.basename(path), path)
        self._wordlist_combo.setCurrentIndex(self._wordlist_combo.count() - 1)

    def _show_tools(self) -> None:
        _ToolsDialog(self).exec_()
        self._refresh_tools()   # a freshly-installed tool enables its backend immediately

    def _show_catalog(self) -> None:
        _WordlistCatalogDialog(self).exec_()
        self._refresh_wordlists()   # a freshly-installed list appears in the picker immediately

    # ── run ──────────────────────────────────────────────────────────
    def _on_run(self) -> None:
        capture = self._capture_edit.text().strip()
        wordlist = self._wordlist_combo.currentData() or ""
        backend = self._backend_combo.currentText()
        if backend not in ("native", "hashcat", "aircrack"):
            QMessageBox.warning(self, "Crack Lab", "Choose a crack engine.")
            return
        try:
            # validate_crack_input accepts a prebuilt .hc22000 hashfile (hashcat-only) as well as a raw
            # capture, so the advertised prebuilt-hashfile path actually runs instead of being rejected.
            cp.validate_crack_input(capture, backend)
            cp.validate_wordlist(wordlist)
        except ValueError as exc:
            QMessageBox.warning(self, "Crack Lab", str(exc))
            return
        # per-run consent affirmation — always shown, never bypassed
        if QMessageBox.question(
                self, "Authorization required", cp.consent_prompt_text(),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._log.clear()
        self._result_label.setText("running…")
        self._run_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._worker = _CrackWorker(capture, wordlist, backend, self._bssid_edit.text().strip())
        self._worker.line.connect(self._append_line)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_stop(self) -> None:
        w = self._worker
        if not (w and w.isRunning()):
            return
        self._append_line("[stop] requested — cancelling…")
        # request_stop() now cancels EVERY backend cleanly: native exits on the stop flag; aircrack/hashcat
        # have their child process killed. The worker then finishes and its done signal resets the buttons
        # + label — no QThread.terminate() (which orphaned the child process and skipped temp cleanup).
        w.request_stop()
        self._stop_btn.setEnabled(False)
        self._result_label.setText("stopping…")

    def shutdown(self) -> None:
        """Stop + join the crack worker so its QThread isn't destroyed mid-run when the app closes
        (that aborts with 'QThread: Destroyed while thread is still running'). Called from
        MainWindow.closeEvent, mirroring the other worker-owning tabs."""
        w = self._worker
        if w is None:
            return
        try:
            if w.isRunning():
                w.request_stop()
                if not w.wait(2000):
                    w.terminate()
                    w.wait(2000)
        except RuntimeError:  # underlying C++ object already gone
            pass

    def _append_line(self, text: str) -> None:
        self._log.appendPlainText(text.rstrip("\n"))

    def _on_done(self, result) -> None:  # result: cp.CrackResult
        self._reset_run_buttons()
        if result.cracked:
            who = result.ssid or result.bssid or "network"
            self._result_label.setText(f"✓ KEY FOUND for {who}: {result.password}")
        else:
            self._result_label.setText(f"no key recovered — {result.detail or 'not in wordlist'}")
        # Durable capture <-> outcome link: if this run came from double-clicking a logged capture,
        # write the recovered key back onto its record (turns the row green via capture.cracked).
        if result.cracked and self._captures is not None and self._active_capture_key:
            # Record the wordlist the run ACTUALLY used (captured on the worker at launch), not whatever
            # the combo shows now — during a run the wordlist selector and its Refresh button stay live,
            # so re-reading the widget could stamp the record with a list that did NOT recover the key.
            wordlist = self._worker._wordlist if self._worker is not None else ""
            self._captures.mark_cracked(
                self._active_capture_key, result.password, result.detail or "", wordlist)
            self._forget_active_capture()   # one write-back per load; a re-run must re-bind

    def _reset_run_buttons(self) -> None:
        # Reuse the backends cached by _refresh_tools instead of re-running detect_tools() (which spawns
        # tool subprocesses) on the GUI thread after every run. "native" is always available, so the
        # cache is non-empty whenever a run is possible; an explicit "recheck" refreshes it.
        self._run_btn.setEnabled(bool(self._backends_cache))
        self._stop_btn.setEnabled(False)
