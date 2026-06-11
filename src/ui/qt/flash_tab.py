"""Flash tab — firmware flashing UI with progress and batch queue."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.core.device_manager import DeviceManager
from src.core.firmware_vault import FirmwareVault
from src.core.flash_engine import FlashEngine, FirmwareProfile

log = logging.getLogger(__name__)

_PROFILES_DIR = Path(__file__).resolve().parents[3] / "src" / "config" / "profiles"


class _FlashWorker(QThread):
    """Background thread for flashing so the UI stays responsive."""

    progress = pyqtSignal(int, str)  # percent, message
    finished = pyqtSignal(bool)  # success

    def __init__(
        self,
        engine: FlashEngine,
        port: str,
        profile: FirmwareProfile,
    ) -> None:
        super().__init__()
        self._engine = engine
        self._port = port
        self._profile = profile

    def run(self) -> None:
        ok = self._engine.flash(
            self._port,
            self._profile,
            progress_callback=self._on_progress,
        )
        self.finished.emit(ok)

    def _on_progress(self, pct: int, msg: str) -> None:
        self.progress.emit(pct, msg)


class _VariantLoader(QThread):
    """Fetch a profile's selectable firmware variants off the UI thread (hits the network)."""

    loaded = pyqtSignal(list)  # list[dict] of {name, label, chip, url}

    def __init__(self, engine: FlashEngine, profile: FirmwareProfile) -> None:
        super().__init__()
        self._engine = engine
        self._profile = profile

    def run(self) -> None:
        try:
            variants = self._engine.list_variants(self._profile)
        except Exception:  # noqa: BLE001 — a picker must never crash the UI
            variants = []
        self.loaded.emit(variants)


class FlashTab(QWidget):
    """Firmware flashing tab with port/profile selectors, progress bar, and batch queue."""

    def __init__(self, dm: DeviceManager, fe: FlashEngine, vault: FirmwareVault | None = None) -> None:
        super().__init__()
        self._dm = dm
        self._fe = fe
        self._vault = vault or FirmwareVault()
        self._worker: _FlashWorker | None = None
        self._variant_loader: _VariantLoader | None = None
        self._profiles: dict[str, Path] = {}  # display name -> path

        self._build_ui()
        self._refresh_ports()
        self._refresh_profiles()
        # Populate variants for the initial profile, then react to profile changes.
        self._profile_combo.currentIndexChanged.connect(self._reload_variants)
        self._reload_variants()

    # ── Layout ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # ── Top row: port + profile selectors ────────────────────────
        top = QHBoxLayout()

        # Port selector
        port_group = QGroupBox("Port")
        port_layout = QVBoxLayout(port_group)
        self._port_combo = QComboBox()
        self._port_combo.setMinimumWidth(160)
        port_layout.addWidget(self._port_combo)
        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self._refresh_ports)
        port_layout.addWidget(btn_refresh)
        top.addWidget(port_group)

        # Profile selector
        prof_group = QGroupBox("Firmware Profile")
        prof_layout = QVBoxLayout(prof_group)
        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(220)
        prof_layout.addWidget(self._profile_combo)
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self._browse_profile)
        prof_layout.addWidget(btn_browse)
        # Board / variant picker — chip auto-detect can't tell a CYD from a generic ESP32
        # (both are 'esp32'); the wrong variant flashes the wrong display driver -> white screen.
        prof_layout.addWidget(QLabel("Board / variant:"))
        self._variant_combo = QComboBox()
        self._variant_combo.setMinimumWidth(220)
        self._variant_combo.setToolTip(
            "Pick your exact board. 'Auto' uses the firmware's per-chip default, which may be wrong "
            "for display boards (CYD/M5/etc.) — if your screen stays blank after flashing, choose the "
            "matching variant here and re-flash."
        )
        self._variant_combo.addItem("Auto (default for chip)", "")
        prof_layout.addWidget(self._variant_combo)
        top.addWidget(prof_group)

        # Flash + Backup buttons
        btn_col = QVBoxLayout()
        self._btn_flash = QPushButton("Flash")
        self._btn_flash.setMinimumHeight(40)
        self._btn_flash.setStyleSheet(
            "QPushButton { background-color: #39ff14; color: #000; font-weight: bold; }"
            "QPushButton:disabled { background-color: #555; color: #888; }"
        )
        self._btn_flash.clicked.connect(self._on_flash)
        btn_col.addWidget(self._btn_flash)

        self._btn_backup = QPushButton("Backup")
        self._btn_backup.clicked.connect(self._on_backup)
        btn_col.addWidget(self._btn_backup)

        self._btn_erase = QPushButton("Erase Flash")
        self._btn_erase.setStyleSheet(
            "QPushButton { color: #ff4444; }"
        )
        self._btn_erase.clicked.connect(self._on_erase)
        btn_col.addWidget(self._btn_erase)

        top.addLayout(btn_col)
        root.addLayout(top)

        # ── Progress bar ─────────────────────────────────────────────
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        root.addWidget(self._progress)

        # ── Bottom: log output + batch queue ─────────────────────────
        bottom = QHBoxLayout()

        # Log output
        log_group = QGroupBox("Flash Log")
        log_layout = QVBoxLayout(log_group)
        self._log_output = QTextEdit()
        self._log_output.setReadOnly(True)
        self._log_output.setFont(QFont("Consolas", 9))
        self._log_output.setStyleSheet("background-color: #111; color: #39ff14;")
        log_layout.addWidget(self._log_output)
        bottom.addWidget(log_group, stretch=3)

        # Batch queue
        queue_group = QGroupBox("Batch Queue")
        queue_layout = QVBoxLayout(queue_group)
        self._queue_list = QListWidget()
        queue_layout.addWidget(self._queue_list)
        btn_add = QPushButton("Add to Queue")
        btn_add.clicked.connect(self._add_to_queue)
        queue_layout.addWidget(btn_add)
        btn_clear = QPushButton("Clear Queue")
        btn_clear.clicked.connect(self._queue_list.clear)
        queue_layout.addWidget(btn_clear)
        bottom.addWidget(queue_group, stretch=1)

        root.addLayout(bottom)

        # ── Firmware Vault section ───────────────────────────────────
        vault_group = QGroupBox("Firmware Vault (Offline Cache)")
        vault_layout = QHBoxLayout(vault_group)

        self._vault_status = QLabel("No cached firmware")
        self._vault_status.setStyleSheet("color: #888;")
        vault_layout.addWidget(self._vault_status, stretch=2)

        btn_download = QPushButton("Download to Vault")
        btn_download.clicked.connect(self._on_vault_download)
        vault_layout.addWidget(btn_download)

        btn_clear_vault = QPushButton("Clear Cache")
        btn_clear_vault.setStyleSheet("QPushButton { color: #ff8c00; }")
        btn_clear_vault.clicked.connect(self._on_vault_clear)
        vault_layout.addWidget(btn_clear_vault)

        root.addWidget(vault_group)
        self._refresh_vault_status()

    # ── Refreshers ───────────────────────────────────────────────────

    def _refresh_ports(self) -> None:
        self._port_combo.clear()
        for dev in self._dm.scan_ports():
            self._port_combo.addItem(f"{dev.port} — {dev.name}", dev.port)

    def _refresh_profiles(self) -> None:
        self._profile_combo.clear()
        self._profiles.clear()
        if _PROFILES_DIR.is_dir():
            for f in sorted(_PROFILES_DIR.glob("*.json")):
                try:
                    p = FirmwareProfile.from_file(f)
                    name = p.name or f.stem
                except Exception:
                    name = f.stem
                self._profiles[name] = f
                self._profile_combo.addItem(name)

    def _reload_variants(self) -> None:
        """Load the selected profile's board variants in the background and repopulate the picker."""
        self._variant_combo.clear()
        self._variant_combo.addItem("Auto (default for chip)", "")
        profile_name = self._profile_combo.currentText()
        profile_path = self._profiles.get(profile_name)
        if not profile_path:
            return
        try:
            profile = FirmwareProfile.from_file(profile_path)
        except Exception:
            return
        self._variant_combo.addItem("Loading variants…", "")
        self._variant_combo.model().item(1).setEnabled(False)
        self._variant_loader = _VariantLoader(self._fe, profile)
        self._variant_loader.loaded.connect(self._on_variants_loaded)
        self._variant_loader.start()

    def _on_variants_loaded(self, variants: list) -> None:
        # Ignore results from a superseded loader (rapid profile switching) so a late-arriving
        # stale list can't repopulate the picker for the wrong profile.
        if self.sender() is not self._variant_loader:
            return
        # Drop the "Loading…" placeholder, keep "Auto" at index 0.
        for i in range(self._variant_combo.count() - 1, 0, -1):
            self._variant_combo.removeItem(i)
        for v in variants:
            label = v.get("label") or v.get("name", "")
            self._variant_combo.addItem(f"{label}  ({v.get('name', '')})", v.get("name", ""))

    def _browse_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select firmware profile", "", "JSON Files (*.json)"
        )
        if path:
            p = Path(path)
            try:
                prof = FirmwareProfile.from_file(p)
                name = prof.name or p.stem
            except Exception:
                name = p.stem
            self._profiles[name] = p
            self._profile_combo.addItem(name)
            self._profile_combo.setCurrentText(name)

    # ── Actions ──────────────────────────────────────────────────────

    def _on_flash(self) -> None:
        port = self._port_combo.currentData()
        profile_name = self._profile_combo.currentText()
        if not port:
            self._log("No port selected.")
            return
        profile_path = self._profiles.get(profile_name)
        if not profile_path:
            self._log("No firmware profile selected.")
            return

        profile = self._fe.load_profile(profile_path)
        variant = self._variant_combo.currentData() or ""
        profile.variant = variant
        if variant:
            self._log(f"Flashing {profile.name} [{variant}] to {port}...")
        else:
            self._log(f"Flashing {profile.name} to {port}...")
        self._btn_flash.setEnabled(False)
        self._progress.setValue(0)

        self._worker = _FlashWorker(self._fe, port, profile)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_flash_done)
        self._worker.start()

    def _on_backup(self) -> None:
        port = self._port_combo.currentData()
        if not port:
            self._log("No port selected.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save backup", f"backup_{port.replace('/', '_')}.bin", "Binary (*.bin)"
        )
        if path:
            self._log(f"Backing up flash from {port} to {path}...")
            ok = self._fe.backup(port, path, progress_callback=self._on_progress_sync)
            self._log("Backup complete." if ok else "Backup failed.")

    def _on_erase(self) -> None:
        port = self._port_combo.currentData()
        if not port:
            self._log("No port selected.")
            return
        self._log(f"Erasing flash on {port}...")

    def _add_to_queue(self) -> None:
        port = self._port_combo.currentData()
        profile_name = self._profile_combo.currentText()
        if port and profile_name:
            item = QListWidgetItem(f"{port} -> {profile_name}")
            item.setData(Qt.UserRole, (port, profile_name))
            self._queue_list.addItem(item)

    # ── Progress / completion ────────────────────────────────────────

    def _on_progress(self, pct: int, msg: str) -> None:
        self._progress.setValue(pct)
        self._log(msg)

    def _on_progress_sync(self, pct: int, msg: str) -> None:
        self._progress.setValue(pct)
        self._log(msg)

    def _on_flash_done(self, success: bool) -> None:
        self._btn_flash.setEnabled(True)
        if success:
            self._progress.setValue(100)
            self._log("Flash completed successfully.")
        else:
            self._log("Flash failed — see log for details.")

    # ── Logging ──────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        self._log_output.append(msg)
        log.info("FlashTab: %s", msg)

    # ── Firmware Vault ───────────────────────────────────────────────

    def _refresh_vault_status(self) -> None:
        """Update the vault status label with cached firmware info."""
        cached = self._vault.list_cached()
        if cached:
            total = sum(len(v) for v in cached.values())
            size_mb = self._vault.vault_size_bytes() / (1024 * 1024)
            profiles = ", ".join(cached.keys())
            self._vault_status.setText(
                f"Cached: {total} version(s) across {len(cached)} profile(s) "
                f"({size_mb:.1f} MB) — {profiles}"
            )
            self._vault_status.setStyleSheet("color: #39ff14;")
        else:
            self._vault_status.setText("No cached firmware")
            self._vault_status.setStyleSheet("color: #888;")

    def _on_vault_download(self) -> None:
        """Download the currently selected profile's firmware to the vault."""
        profile_name = self._profile_combo.currentText()
        profile_path = self._profiles.get(profile_name)
        if not profile_path:
            self._log("No firmware profile selected for vault download.")
            return

        # Load profile to get ID
        try:
            import json
            data = json.loads(profile_path.read_text(encoding="utf-8"))
            profile_id = data.get("id", profile_path.stem)
        except Exception:
            profile_id = profile_path.stem

        self._log(f"Downloading {profile_name} to vault...")

        def progress_cb(downloaded, total, msg):
            if total > 0:
                pct = int((downloaded / total) * 100)
                self._progress.setValue(pct)

        # Run download in background thread
        import threading

        def _do_download():
            result = self._vault.download_firmware(
                profile_id, progress_callback=progress_cb
            )
            if result:
                self._log(f"Vault: downloaded {profile_name} -> {result}")
            else:
                self._log(f"Vault: download failed for {profile_name}")
            self._refresh_vault_status()

        threading.Thread(target=_do_download, daemon=True).start()

    def _on_vault_clear(self) -> None:
        """Clear the firmware vault cache."""
        deleted = self._vault.clear_cache()
        self._log(f"Vault: cleared {deleted} cached file(s)")
        self._refresh_vault_status()
