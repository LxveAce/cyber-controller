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

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
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
    QVBoxLayout,
    QWidget,
)

from src.core import crack_pipeline as cp
from src.core import wordlist_manager as wm

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

    def run(self) -> None:  # noqa: D401 - QThread entry point
        emit = self.line.emit
        try:
            tools = cp.detect_tools()
            if self._backend == "aircrack":
                result = cp.run_aircrack(self._capture, self._wordlist, emit,
                                         tools=tools, bssid=self._bssid)
            else:
                # hashcat path: convert the capture to .hc22000 first (unless already one)
                if os.path.splitext(self._capture)[1].lower() == ".hc22000":
                    hash_file = self._capture
                else:
                    fd, hash_file = tempfile.mkstemp(suffix=".hc22000", prefix="cc_wifi_")
                    os.close(fd)
                    n = cp.convert_capture(self._capture, hash_file, emit, tools=tools)
                    emit(f"[convert] {n} crackable hash(es) extracted.")
                result = cp.run_hashcat(hash_file, self._wordlist, emit, tools=tools)
        except Exception as exc:  # never let a worker exception kill the thread silently
            log.exception("wifi-audit crack worker failed")
            result = cp.CrackResult(detail=f"error: {exc}")
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
        w = self._worker
        if w is not None and w.isRunning():
            w.terminate()   # like the crack worker's stop; a killed run leaves only a .part temp
            w.wait(2000)
        super().closeEvent(event)


class CrackLabTab(QWidget):
    """Reachable UI for the offline WPA dictionary attack (capture -> wordlist -> crack)."""

    def __init__(self) -> None:
        super().__init__()
        self._worker: _CrackWorker | None = None
        root = QVBoxLayout(self)

        info = QLabel(cp.capability_text())
        info.setWordWrap(True)
        root.addWidget(info)

        # tools presence
        tools_box = QGroupBox("Cracking tools (you install these — CC never bundles them)")
        tl = QHBoxLayout(tools_box)
        self._tools_label = QLabel("…")
        self._tools_label.setWordWrap(True)
        tl.addWidget(self._tools_label, 1)
        recheck = QPushButton("Re-check")
        recheck.clicked.connect(self._refresh_tools)
        tl.addWidget(recheck)
        root.addWidget(tools_box)

        # capture picker
        cap_row = QHBoxLayout()
        cap_row.addWidget(QLabel("Capture:"))
        self._capture_edit = QLineEdit()
        self._capture_edit.setPlaceholderText("a .pcapng/.pcap/.cap/.hc22000 file you captured")
        cap_row.addWidget(self._capture_edit, 1)
        browse_cap = QPushButton("Browse…")
        browse_cap.clicked.connect(self._pick_capture)
        cap_row.addWidget(browse_cap)
        root.addLayout(cap_row)

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

    # ── populate ─────────────────────────────────────────────────────
    def _refresh_tools(self) -> None:
        tools = cp.detect_tools()
        parts = []
        for st in tools.values():
            mark = "✓" if st.present else "✗"
            parts.append(f"{mark} {st.name}" + (f" ({st.version})" if st.version else ""))
        self._tools_label.setText("   ".join(parts) or "no tools detected")
        backends = cp.available_backends(tools)
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

    def _show_catalog(self) -> None:
        _WordlistCatalogDialog(self).exec_()
        self._refresh_wordlists()   # a freshly-installed list appears in the picker immediately

    # ── run ──────────────────────────────────────────────────────────
    def _on_run(self) -> None:
        capture = self._capture_edit.text().strip()
        wordlist = self._wordlist_combo.currentData() or ""
        backend = self._backend_combo.currentText()
        if backend not in ("hashcat", "aircrack"):
            QMessageBox.warning(self, "Crack Lab", "Install hashcat or aircrack-ng first.")
            return
        try:
            cp.validate_capture(capture)
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
        if self._worker and self._worker.isRunning():
            self._append_line("[stop] requested — terminating the run…")
            self._worker.terminate()
            self._worker.wait(3000)
            self._reset_run_buttons()
            self._result_label.setText("stopped")

    def _append_line(self, text: str) -> None:
        self._log.appendPlainText(text.rstrip("\n"))

    def _on_done(self, result) -> None:  # result: cp.CrackResult
        self._reset_run_buttons()
        if result.cracked:
            who = result.ssid or result.bssid or "network"
            self._result_label.setText(f"✓ KEY FOUND for {who}: {result.password}")
        else:
            self._result_label.setText(f"no key recovered — {result.detail or 'not in wordlist'}")

    def _reset_run_buttons(self) -> None:
        self._run_btn.setEnabled(bool(cp.available_backends(cp.detect_tools())))
        self._stop_btn.setEnabled(False)
