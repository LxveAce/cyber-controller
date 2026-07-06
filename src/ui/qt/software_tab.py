"""Software-OS tab — flash bootable PC/USB operating systems (Kali / Tails / Arch / ...).

Separate from the firmware Flash tab: this writes whole-disk OS images to a removable USB. It drives
the verified, auto-resolving catalog in :mod:`src.core.os_catalog` (latest version online, bundled
pinned version offline) and reuses the hardened removable-only writer. The destructive write happens
off the UI thread; every step is logged.
"""

from __future__ import annotations

import logging
import os
import tempfile

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.core import os_catalog as oc
from src.core.backends import sd_backend as sd
from src.ui.qt.flash_tab import _make_card  # reuse the card factory

log = logging.getLogger(__name__)


class _ResolveWorker(QThread):
    """Resolve the latest release for an OS entry off the UI thread (hits the network)."""

    done = pyqtSignal(object, str)  # Resolved | None, log text

    def __init__(self, entry: oc.OSImage, offline: bool) -> None:
        super().__init__()
        self._entry = entry
        self._offline = offline

    def run(self) -> None:
        lines: list[str] = []
        try:
            r = oc.resolve(self._entry, lines.append, online=not self._offline)
        except Exception as exc:  # noqa: BLE001 — resolution must never crash the UI
            r = None
            lines.append(f"resolve failed: {exc}")
        self.done.emit(r, "\n".join(lines))


class _OSFlashWorker(QThread):
    """Download (if needed) + verify + write an OS image to a removable device."""

    progress = pyqtSignal(int, str)  # pct (-1 = log only), message
    finished = pyqtSignal(bool)

    def __init__(self, entry: oc.OSImage, resolved: oc.Resolved, device: str,
                 local_image: str | None = None, local_sig: str | None = None) -> None:
        super().__init__()
        self._entry = entry
        self._resolved = resolved
        self._device = device
        self._local_image = local_image
        self._local_sig = local_sig

    def run(self) -> None:
        def on(s: str) -> None:
            self.progress.emit(-1, s)

        def prog(f: float) -> None:
            self.progress.emit(int(f * 100), "")

        entry, r = self._entry, self._resolved
        img, sig = self._local_image, self._local_sig
        sums = sums_sig = None
        cache = os.path.join(tempfile.gettempdir(), f"cc_os_{entry.id}")
        try:
            if not img:
                img = oc.download(r.image_url, cache, on, prog)
                if r.verify_model == "image_sig" and r.sig_url and not sig:
                    try:
                        sig = oc.download(r.sig_url, cache, on)
                    except Exception as exc:  # noqa: BLE001
                        on(f"[os] signature fetch failed ({exc}); will fall back to SHA-256.")
                # Parrot-style image_sig: fetch the clearsigned hashes file so its signature is verified
                # before the SHA it carries is trusted.
                if r.verify_model == "image_sig" and r.checksums_url and not sums:
                    try:
                        sums = oc.download(r.checksums_url, cache, on)
                    except Exception as exc:  # noqa: BLE001
                        on(f"[os] signed hashes fetch failed ({exc}).")
                if r.verify_model == "checksums_sig":
                    if r.checksums_url:
                        sums = oc.download(r.checksums_url, cache, on)
                    if r.checksums_sig_url:
                        try:
                            sums_sig = oc.download(r.checksums_sig_url, cache, on)
                        except Exception as exc:  # noqa: BLE001
                            on(f"[os] SHA256SUMS signature fetch failed ({exc}).")
            rc = oc.flash_os_image(entry, r, img, self._device, on, prog, sig_path=sig,
                                   checksums_path=sums, checksums_sig_path=sums_sig, confirmed=True)
            self.finished.emit(rc == 0)
        except Exception as exc:  # noqa: BLE001
            on(f"[os] ERROR: {exc}")
            self.finished.emit(False)


class SoftwareTab(QWidget):
    """Flash a bootable OS (Kali / Tails / Arch / ...) to a removable USB stick."""

    def __init__(self) -> None:
        super().__init__()
        self._entries: list[oc.OSImage] = []
        self._resolved: oc.Resolved | None = None
        self._local_image: str | None = None
        self._resolver: _ResolveWorker | None = None
        self._worker: _OSFlashWorker | None = None
        self._build_ui()
        self._load_catalog()
        self._refresh_drives()

    # ── layout ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        container = QWidget()
        root = QVBoxLayout(container)

        intro = QLabel("Write a verified bootable operating system to a USB stick. Firmware for boards "
                       "lives on the Flash tab — this tab is for PC/USB operating systems.")
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        root.addWidget(intro)

        top = QHBoxLayout()
        top.setSpacing(8)

        os_card, os_layout = _make_card("Operating System")
        self._os_combo = QComboBox()
        self._os_combo.setMinimumWidth(200)
        self._os_combo.setToolTip("Pick the OS to write to USB. Each is downloaded from its official "
                                  "source and integrity-verified (SHA-256 + OpenPGP signature) before writing.")
        self._os_combo.currentIndexChanged.connect(self._on_os_changed)
        os_layout.addWidget(self._os_combo)
        self._os_desc = QLabel("")
        self._os_desc.setObjectName("muted")
        self._os_desc.setWordWrap(True)
        os_layout.addWidget(self._os_desc)
        self._offline_cb = QCheckBox("Use bundled version (offline)")
        self._offline_cb.setToolTip("When unchecked, the latest version is resolved live from the OS "
                                    "project. Check this to flash the version bundled with the app "
                                    "(no internet needed).")
        os_layout.addWidget(self._offline_cb)
        self._btn_check = QPushButton("Check latest")
        self._btn_check.setToolTip("Resolve the current version + download/verification URLs from the "
                                   "official source (or the bundled version when offline).")
        self._btn_check.clicked.connect(self._on_check)
        os_layout.addWidget(self._btn_check)
        self._os_status = QLabel("No version resolved yet.")
        self._os_status.setObjectName("muted")
        self._os_status.setWordWrap(True)
        os_layout.addWidget(self._os_status)
        top.addWidget(os_card, stretch=2)

        drive_card, drive_layout = _make_card("Target USB (removable)")
        self._drive_combo = QComboBox()
        self._drive_combo.setMinimumWidth(200)
        self._drive_combo.setToolTip("Only removable drives are listed. THE ENTIRE DRIVE IS ERASED.")
        drive_layout.addWidget(self._drive_combo)
        btn_refresh = QPushButton("Refresh drives")
        btn_refresh.clicked.connect(self._refresh_drives)
        drive_layout.addWidget(btn_refresh)
        self._btn_local = QPushButton("Use local image…")
        self._btn_local.setToolTip("Flash an OS image (.iso/.img) you already downloaded instead of fetching it.")
        self._btn_local.clicked.connect(self._browse_local)
        drive_layout.addWidget(self._btn_local)
        self._local_lbl = QLabel("")
        self._local_lbl.setObjectName("muted")
        self._local_lbl.setWordWrap(True)
        drive_layout.addWidget(self._local_lbl)
        top.addWidget(drive_card, stretch=2)

        btn_col = QVBoxLayout()
        self._btn_flash = QPushButton("Flash OS")
        self._btn_flash.setObjectName("flash_btn")
        self._btn_flash.setMinimumHeight(40)
        self._btn_flash.setToolTip("Download (if needed), verify, then write the OS to the selected "
                                   "removable USB. Destructive — the whole drive is erased.")
        self._btn_flash.clicked.connect(self._on_flash)
        btn_col.addWidget(self._btn_flash)
        top.addLayout(btn_col)
        root.addLayout(top)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setMinimumHeight(20)
        root.addWidget(self._progress)

        log_card, log_layout = _make_card("Log")
        self._log_output = QTextEdit()
        self._log_output.setReadOnly(True)
        self._log_output.setObjectName("terminal")
        self._log_output.setMinimumHeight(140)
        log_layout.addWidget(self._log_output)
        root.addWidget(log_card, stretch=1)

        scroll.setWidget(container)
        outer.addWidget(scroll)

    # ── data ─────────────────────────────────────────────────────────

    def _load_catalog(self) -> None:
        try:
            self._entries = oc.load_catalog()
        except Exception as exc:  # noqa: BLE001
            self._log(f"Could not load OS catalog: {exc}")
            self._entries = []
        self._os_combo.clear()
        for e in self._entries:
            self._os_combo.addItem(f"{e.name}  [{e.category}]", e.id)
        self._on_os_changed()

    def _current_entry(self) -> oc.OSImage | None:
        oid = self._os_combo.currentData()
        return next((e for e in self._entries if e.id == oid), None)

    def _on_os_changed(self) -> None:
        self._resolved = None
        e = self._current_entry()
        if e:
            self._os_desc.setText(f"{e.description}  ({e.image_type.upper()}, verify: {e.verify_model})")
            self._os_status.setText(f"Bundled version: {e.pinned.get('version', '?')}. "
                                    "Click 'Check latest' to resolve the current release.")

    def _refresh_drives(self) -> None:
        self._drive_combo.clear()
        try:
            for c in sd.detect_sd_cards(lambda *_: None):
                gb = (c.get("size") or 0) / (1 << 30)
                self._drive_combo.addItem(f"{c['device']}  {c.get('name', '')}  {gb:.1f} GB", c["device"])
        except Exception as exc:  # noqa: BLE001
            self._log(f"Drive scan failed: {exc}")
        if self._drive_combo.count() == 0:
            self._drive_combo.addItem("No removable drives found", None)

    # ── Dual-depth (Simple / Pro) ────────────────────────────────────

    def set_ui_mode(self, mode: str) -> None:
        """Simple = always fetch the latest release online; hide the offline toggle + local-image
        override. Pro restores both. Offline operation itself is never disabled — Simple just hides the
        *toggle* and defaults to online (per the offline-first invariant)."""
        pro = str(mode).lower() != "simple"
        for w in (getattr(self, "_offline_cb", None), getattr(self, "_btn_local", None),
                  getattr(self, "_local_lbl", None)):
            if w is not None:
                w.setVisible(pro)
        if not pro:
            if getattr(self, "_offline_cb", None) is not None:
                self._offline_cb.setChecked(False)  # always online in Simple
            self._local_image = None  # always fetch latest, no local override
            if getattr(self, "_local_lbl", None) is not None:
                self._local_lbl.setText("")

    def _browse_local(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select an OS image", "",
                                              "Disk images (*.iso *.img);;All files (*)")
        if path:
            self._local_image = path
            self._local_lbl.setText(f"Local image: {os.path.basename(path)}")
        else:
            self._local_image = None
            self._local_lbl.setText("")

    # ── actions ──────────────────────────────────────────────────────

    def _on_check(self) -> None:
        e = self._current_entry()
        if not e:
            return
        self._btn_check.setEnabled(False)
        self._os_status.setText("Resolving…")
        self._resolver = _ResolveWorker(e, self._offline_cb.isChecked())
        self._resolver.done.connect(self._on_resolved)
        self._resolver.start()

    def _on_resolved(self, resolved, log_text: str) -> None:
        self._btn_check.setEnabled(True)
        if log_text:
            self._log(log_text)
        self._resolved = resolved
        if resolved is None:
            self._os_status.setText("Could not resolve a version.")
            return
        self._os_status.setText(f"{resolved.version}  (source: {resolved.source}, "
                                f"verify: {resolved.verify_model})")

    def _on_flash(self) -> None:
        e = self._current_entry()
        if not e:
            return
        device = self._drive_combo.currentData()
        if not device:
            self._log("No removable drive selected.")
            return
        if self._resolved is None:
            # resolve synchronously-ish: kick off check first
            self._log("Resolving version before flashing… click Flash OS again once resolved.")
            self._on_check()
            return
        name = self._drive_combo.currentText()
        if QMessageBox.warning(
            self, "Erase and flash?",
            f"This will ERASE EVERYTHING on:\n\n    {name}\n\nand write {e.name} {self._resolved.version}.\n\n"
            "Continue?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            self._log("Flash cancelled.")
            return
        self._btn_flash.setEnabled(False)
        self._progress.setValue(0)
        self._log(f"Flashing {e.name} {self._resolved.version} -> {device} …")
        self._worker = _OSFlashWorker(e, self._resolved, device, local_image=self._local_image)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_done)
        self._worker.start()

    def _on_progress(self, pct: int, msg: str) -> None:
        if pct >= 0:
            self._progress.setValue(pct)
        if msg:
            self._log(msg)

    def _on_done(self, ok: bool) -> None:
        self._btn_flash.setEnabled(True)
        if ok:
            self._progress.setValue(100)
            self._log("OS flash completed successfully — boot the target machine from this USB.")
        else:
            self._log("OS flash failed — see log above.")

    def _log(self, msg: str) -> None:
        self._log_output.append(msg)
        log.info("SoftwareTab: %s", msg)
