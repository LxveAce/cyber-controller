"""Flash tab — firmware flashing UI with progress and batch queue."""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFont
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import load_settings
from src.core.device_manager import DeviceManager
from src.core.firmware_vault import FirmwareVault, configured_vault_dir
from src.core.flash_engine import (
    FirmwareProfile,
    FlashEngine,
    _PortBusy,
    chip_match,
    supported_boards_text,
)
from src.core.resources import resource_path
from src.models.device import BoardType

# Chips we can trust from USB enumeration ALONE (no port probe). Only these native-USB
# sub-variants are unambiguous; the plain ESP32 bucket collapses classic ESP32 / S2 /
# ESP8266 / BW16 behind a shared UART bridge, so it stays unknown (neutral, never red).
_BOARDTYPE_CHIP = {
    BoardType.ESP32_S3: "esp32s3",
    BoardType.ESP32_S2: "esp32s2",
    BoardType.ESP32_C3: "esp32c3",
}

log = logging.getLogger(__name__)

_PROFILES_DIR = resource_path("src", "config", "profiles")


def _make_card(title: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    """Create a card-styled QFrame with optional title label."""
    card = QFrame()
    card.setObjectName("card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(8)
    if title:
        lbl = QLabel(title)
        lbl.setObjectName("card_title")
        layout.addWidget(lbl)
    return card, layout


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


class _DetectWorker(QThread):
    """Flash the CYD probe and read back the panel identity, off the UI thread.

    Runs through ``FlashEngine.detect_cyd`` so the probe-flash reserves the port in the same
    busy-guard as flash/backup/erase — a Detect can no longer race a concurrent esptool onto one
    port (a brick path). If the port is already busy the guard raises and we surface it cleanly.
    """

    progress = pyqtSignal(str)
    done = pyqtSignal(object)  # CydResult
    failed = pyqtSignal(str)

    def __init__(self, engine: FlashEngine, port: str) -> None:
        super().__init__()
        self._engine = engine
        self._port = port

    def run(self) -> None:
        try:
            result = self._engine.detect_cyd(self._port, progress=self.progress.emit)
            self.done.emit(result)
        except _PortBusy:
            self.failed.emit(f"port {self._port} is busy with another flash/backup/erase")
        except Exception as exc:  # noqa: BLE001 — surface as a log line, never crash the UI
            self.failed.emit(str(exc))


class _VaultWorker(QThread):
    """Download a profile's firmware into the vault off the UI thread (network I/O).

    The vault download used to run on a raw ``threading.Thread`` and call QWidget methods
    (progress bar, log, status label) directly from that thread — undefined behavior in Qt that
    can corrupt state or segfault, most visibly in the frozen build. Everything now flows back to
    the GUI thread via signals.
    """

    progress = pyqtSignal(int)     # percent
    log = pyqtSignal(str)
    done = pyqtSignal(object)      # the downloaded path, or None on failure

    def __init__(self, vault, profile_id: str) -> None:
        super().__init__()
        self._vault = vault
        self._profile_id = profile_id

    def run(self) -> None:
        def _cb(downloaded, total, _msg=""):
            if total and total > 0:
                self.progress.emit(int((downloaded / total) * 100))
        try:
            result = self._vault.download_firmware(self._profile_id, progress_callback=_cb)
        except Exception as exc:  # noqa: BLE001 — surface, never crash the UI
            self.log.emit(f"Vault: download error: {exc}")
            result = None
        self.done.emit(result)


class _OpWorker(QThread):
    """Run a blocking serial op (backup / erase) off the UI thread so the window can't freeze.

    ``fn`` takes a ``(pct, msg)`` progress callback and returns a truthy success flag.
    """

    progress = pyqtSignal(int, str)
    done = pyqtSignal(bool)

    def __init__(self, fn) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            ok = bool(self._fn(lambda pct, msg: self.progress.emit(pct, msg)))
        except Exception:  # noqa: BLE001 — a failed op reports False, never crashes the UI
            ok = False
        self.done.emit(ok)


class FlashTab(QWidget):
    """Firmware flashing tab with port/profile selectors, progress bar, and batch queue."""

    def __init__(self, dm: DeviceManager, fe: FlashEngine, vault: FirmwareVault | None = None) -> None:
        super().__init__()
        self._dm = dm
        self._fe = fe
        self._vault = vault or FirmwareVault(configured_vault_dir())
        self._worker: _FlashWorker | None = None
        self._variant_loader: _VariantLoader | None = None
        self._detect_worker: _DetectWorker | None = None
        # Strong refs to in-flight background QThreads so CPython can't GC one mid-run (which
        # aborts with "QThread: Destroyed while thread is still running"). Each worker removes
        # itself on its finished signal.
        self._bg_workers: "set[QThread]" = set()
        self._op_worker: _OpWorker | None = None
        # Batch-queue sequential-flash state (see _on_flash_queue). Jobs are flashed one at a time by
        # chaining each _FlashWorker's finished signal to the next, reusing the exact single-flash path.
        self._batch_worker: _FlashWorker | None = None
        self._batch_jobs: list[tuple[str, str]] = []
        self._batch_idx = 0
        self._batch_ok = 0
        self._pending_variant: str | None = None  # variant to select once the async list lands
        self._profiles: dict[str, Path] = {}  # display name -> path
        self._profile_objs: dict[str, FirmwareProfile] = {}  # display name -> loaded profile

        self._build_ui()
        self._refresh_ports()
        self._refresh_profiles()
        # Populate variants for the initial profile, then react to profile changes.
        self._profile_combo.currentIndexChanged.connect(self._reload_variants)
        self._reload_variants()
        # Re-hint firmware compatibility whenever the selected port changes.
        self._port_combo.currentIndexChanged.connect(self._recolor_profiles)

    # ── Layout ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        root = QVBoxLayout(container)

        # ── Top row: port + profile selectors ────────────────────────
        top = QHBoxLayout()
        top.setSpacing(8)

        # Port selector card
        port_card, port_layout = _make_card("Port")
        self._port_combo = QComboBox()
        self._port_combo.setMinimumWidth(140)
        port_layout.addWidget(self._port_combo)
        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self._refresh_ports)
        port_layout.addWidget(btn_refresh)
        top.addWidget(port_card, stretch=1)

        # Profile selector card
        prof_card, prof_layout = _make_card("Firmware Profile")
        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(160)
        prof_layout.addWidget(self._profile_combo)
        self._btn_browse = QPushButton("Browse...")
        self._btn_browse.clicked.connect(self._browse_profile)
        prof_layout.addWidget(self._btn_browse)
        # Board / variant picker
        self._variant_label = QLabel("Board / variant:")
        self._variant_label.setObjectName("muted")
        self._variant_label.setWordWrap(True)
        prof_layout.addWidget(self._variant_label)
        self._variant_combo = QComboBox()
        self._variant_combo.setMinimumWidth(160)
        self._variant_combo.setToolTip(
            "Pick your exact board. 'Auto' uses the firmware's per-chip default, which may be wrong "
            "for display boards (CYD/M5/etc.) — if your screen stays blank after flashing, choose the "
            "matching variant here and re-flash."
        )
        self._variant_combo.addItem("Auto (default for chip)", "")
        prof_layout.addWidget(self._variant_combo)
        # Detect which board is on the port (identifies CYD panel + variant so the screen isn't blank
        # or mirrored from a wrong build). Flashes a probe — destructive, confirms first.
        self._btn_detect = QPushButton("Detect board (CYD)")
        self._btn_detect.setToolTip(
            "Flash a tiny probe that reads the display controller to identify which board this is "
            "(e.g. CYD 2.8\" ILI9341 vs 2-USB ST7789 vs 3.5\" ST7796), then auto-selects the matching "
            "variant. Overwrites the current firmware — re-flash real firmware after."
        )
        self._btn_detect.clicked.connect(self._on_detect)
        prof_layout.addWidget(self._btn_detect)
        top.addWidget(prof_card, stretch=2)

        # Flash + Backup buttons
        btn_col = QVBoxLayout()
        self._btn_flash = QPushButton("Flash")
        self._btn_flash.setObjectName("flash_btn")
        self._btn_flash.setMinimumHeight(40)
        self._btn_flash.setToolTip(
            "Write the selected firmware profile to the board on the chosen port."
        )
        self._btn_flash.clicked.connect(self._on_flash)
        btn_col.addWidget(self._btn_flash)

        self._btn_backup = QPushButton("Backup")
        self._btn_backup.setToolTip(
            "Read the board's current flash contents to a file so you can restore it later."
        )
        self._btn_backup.clicked.connect(self._on_backup)
        btn_col.addWidget(self._btn_backup)

        self._btn_erase = QPushButton("Erase Flash")
        self._btn_erase.setObjectName("erase_btn")
        self._btn_erase.setToolTip(
            "Wipe the board's entire flash. Destructive — confirms before running."
        )
        self._btn_erase.clicked.connect(self._on_erase)
        btn_col.addWidget(self._btn_erase)

        top.addLayout(btn_col)
        root.addLayout(top)

        # ── Dead Man's Switch card ───────────────────────────────────
        self._suicide_card = QFrame()
        self._suicide_card.setObjectName("card")
        self._suicide_card.setStyleSheet(
            """
            QFrame#suicide_card_active {
                background-color: #161b22;
                border: 2px solid #f0883e;
                border-radius: 8px;
                padding: 8px 16px;
            }
            """
        )
        suicide_layout = QVBoxLayout(self._suicide_card)
        suicide_layout.setContentsMargins(12, 8, 12, 8)
        suicide_layout.setSpacing(4)

        self._suicide_checkbox = QCheckBox("Enable Dead Man's Switch")
        self._suicide_checkbox.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self._suicide_checkbox.toggled.connect(self._on_suicide_toggled)
        suicide_layout.addWidget(self._suicide_checkbox)

        suicide_desc = QLabel(
            "Opens Dead Man's Switch setup and provisions a guardcfg bundle (host-side). Flashing the gate "
            "onto the device is done with `cyber-controller --deadman-setup` — not this flash button yet."
        )
        suicide_desc.setObjectName("muted")
        suicide_desc.setWordWrap(True)
        suicide_desc.setStyleSheet("color: #8b949e; font-size: 9pt; background: transparent;")
        suicide_layout.addWidget(suicide_desc)

        root.addWidget(self._suicide_card)

        # ── Progress bar ─────────────────────────────────────────────
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setMinimumHeight(20)
        root.addWidget(self._progress)

        # ── Bottom: log output + batch queue ─────────────────────────
        bottom = QHBoxLayout()

        # Log output card
        log_card, log_layout = _make_card("Flash Log")
        self._log_output = QTextEdit()
        self._log_output.setReadOnly(True)
        self._log_output.setObjectName("terminal")
        self._log_output.setMinimumHeight(100)
        log_layout.addWidget(self._log_output)
        bottom.addWidget(log_card, stretch=3)

        # Batch queue card
        self._queue_card, queue_layout = _make_card("Batch Queue")
        self._queue_list = QListWidget()
        self._queue_list.setMinimumHeight(60)
        queue_layout.addWidget(self._queue_list)
        btn_add = QPushButton("Add to Queue")
        btn_add.clicked.connect(self._add_to_queue)
        queue_layout.addWidget(btn_add)
        self._btn_flash_queue = QPushButton("Flash Queue")
        self._btn_flash_queue.clicked.connect(self._on_flash_queue)
        queue_layout.addWidget(self._btn_flash_queue)
        btn_clear = QPushButton("Clear Queue")
        btn_clear.clicked.connect(self._queue_list.clear)
        queue_layout.addWidget(btn_clear)
        bottom.addWidget(self._queue_card, stretch=1)

        root.addLayout(bottom, stretch=1)

        # ── Firmware Vault section ───────────────────────────────────
        self._vault_card, vault_layout = _make_card("Firmware Vault (Offline Cache)")
        vault_row = QHBoxLayout()

        self._vault_status = QLabel("No cached firmware")
        self._vault_status.setObjectName("muted")
        self._vault_status.setWordWrap(True)
        vault_row.addWidget(self._vault_status, stretch=2)

        btn_download = QPushButton("Download to Vault")
        btn_download.clicked.connect(self._on_vault_download)
        vault_row.addWidget(btn_download)

        btn_clear_vault = QPushButton("Clear Cache")
        btn_clear_vault.clicked.connect(self._on_vault_clear)
        vault_row.addWidget(btn_clear_vault)

        vault_layout.addLayout(vault_row)
        root.addWidget(self._vault_card)

        scroll.setWidget(container)
        outer.addWidget(scroll)
        self._refresh_vault_status()

    # ── Dual-depth (Simple / Pro) ────────────────────────────────────

    def set_ui_mode(self, mode: str) -> None:
        """Simple = streamline to the essential flash flow (Port, Profile, Flash/Backup/Erase,
        progress, log). Hide the advanced groups: Browse, board/variant picker (locked to Auto),
        Dead Man's Switch, Batch Queue, Firmware Vault. Pro restores everything (today's UI)."""
        pro = str(mode).lower() != "simple"
        for w in (
            getattr(self, "_btn_browse", None), getattr(self, "_variant_label", None),
            getattr(self, "_variant_combo", None), getattr(self, "_btn_detect", None),
            getattr(self, "_suicide_card", None),
            getattr(self, "_queue_card", None), getattr(self, "_vault_card", None),
        ):
            if w is not None:
                w.setVisible(pro)
        if not pro:
            # Lock to the firmware default ("Auto") and make sure the Dead Man's Switch is off when its
            # control is hidden, so a hidden checkbox can't silently arm a destructive flash.
            if getattr(self, "_variant_combo", None) is not None and self._variant_combo.count():
                self._variant_combo.setCurrentIndex(0)
            if getattr(self, "_suicide_checkbox", None) is not None:
                self._suicide_checkbox.setChecked(False)

    # ── Refreshers ───────────────────────────────────────────────────

    def _refresh_ports(self) -> None:
        self._port_combo.clear()
        for dev in self._dm.scan_ports():
            self._port_combo.addItem(f"{dev.port} — {dev.name}", dev.port)
        # Empty-state entry (same shape as software_tab's empty drive combo).
        if self._port_combo.count() == 0:
            self._port_combo.addItem("No ports found — plug in a board and press Refresh", None)
        self._recolor_profiles()

    def _refresh_profiles(self) -> None:
        self._profile_combo.clear()
        self._profiles.clear()
        self._profile_objs.clear()
        if _PROFILES_DIR.is_dir():
            for f in sorted(_PROFILES_DIR.glob("*.json")):
                p = None
                try:
                    p = FirmwareProfile.from_file(f)
                    name = p.name or f.stem
                except Exception:
                    name = f.stem
                self._profiles[name] = f
                self._profile_combo.addItem(name)
                # Show the profile's supported boards on hover. Tooltip only — the item
                # TEXT stays the profile name because everything looks the profile up by
                # _profile_combo.currentText(); changing the label would break that.
                if p is not None:
                    self._profile_objs[name] = p
                    tip = supported_boards_text(p)
                    if tip:
                        self._profile_combo.setItemData(
                            self._profile_combo.count() - 1, tip, Qt.ToolTipRole)
        self._recolor_profiles()

    def _recolor_profiles(self) -> None:
        """Advisory green/red hint on the firmware list for the connected board's chip.

        Foreground colour ONLY — it never disables an item, so any firmware stays
        selectable (this is a hint, not a gate). Colours only when the chip is known
        confidently from USB enumeration; otherwise the item keeps its default colour.
        Never RED on a guess — a shared UART bridge can't tell classic ESP32 / S2 /
        ESP8266 / BW16 apart, so those stay neutral.
        """
        chip = None
        port = self._port_combo.currentData()
        if port:
            for dev in self._dm.scan_ports():
                if dev.port == port:
                    chip = _BOARDTYPE_CHIP.get(dev.board_type)
                    break
        model = self._profile_combo.model()
        green = QBrush(QColor("#3fb950"))
        red = QBrush(QColor("#f85149"))
        for i in range(self._profile_combo.count()):
            item = model.item(i)
            prof = self._profile_objs.get(self._profile_combo.itemText(i))
            verdict = chip_match(chip, prof) if (prof is not None and chip) else "neutral"
            if verdict == "match":
                item.setForeground(green)
            elif verdict == "mismatch":
                item.setForeground(red)
            else:
                item.setData(None, Qt.ForegroundRole)  # reset to the default colour

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
        # Hold a strong ref until it finishes so rapid profile switching can't drop the last
        # reference to a still-running loader (which would abort the process). _on_variants_loaded
        # still uses `sender() is self._variant_loader` to keep latest-wins semantics.
        loader = self._variant_loader
        self._bg_workers.add(loader)
        loader.finished.connect(lambda w=loader: self._bg_workers.discard(w))
        loader.loaded.connect(self._on_variants_loaded)
        loader.start()

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
        # Apply a variant chosen by board-detection now that the real list is in. If detection found a
        # key the fetched list somehow lacks, add it so it stays selectable.
        if self._pending_variant:
            if not self._select_variant(self._pending_variant):
                self._variant_combo.addItem(f"{self._pending_variant}  (detected)", self._pending_variant)
                self._select_variant(self._pending_variant)

    def _browse_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select firmware profile", "", "JSON Files (*.json)"
        )
        if path:
            p = Path(path)
            try:
                prof = FirmwareProfile.from_file(p)
            except Exception as exc:  # noqa: BLE001
                # Don't register a file we couldn't parse — it would become the current, "flashable"
                # selection and then crash the flash path. Reject it here with a clear message.
                self._log(f"Not a valid firmware profile ({p.name}): {exc}")
                return
            name = prof.name or p.stem
            self._profiles[name] = p
            self._profile_combo.addItem(name)
            self._profile_combo.setCurrentText(name)

    # ── Board detection (CYD) ────────────────────────────────────────

    def _on_detect(self) -> None:
        port = self._port_combo.currentData()
        if not port:
            self._log("No port selected.")
            return
        from PyQt5.QtWidgets import QMessageBox

        resp = QMessageBox.warning(
            self, "Detect board",
            f"Detection flashes a small probe to the board on {port} to read its display, "
            "OVERWRITING its current firmware.\n\nYou'll re-flash real firmware afterward. Continue?",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if resp != QMessageBox.Yes:
            self._log("Detection cancelled.")
            return
        self._log(f"Detecting board on {port}…")
        # Detect flashes the probe over serial — disable every button that would launch a second
        # esptool/serial op on the same port (flash, backup, erase) until it finishes. The engine's
        # port-guard is the real safety net; this just keeps the UI from inviting the collision.
        self._set_detect_busy(True)
        self._detect_worker = _DetectWorker(self._fe, port)
        self._detect_worker.progress.connect(self._log)
        self._detect_worker.done.connect(self._on_detect_done)
        self._detect_worker.failed.connect(self._on_detect_failed)
        self._detect_worker.start()

    def _set_detect_busy(self, busy: bool) -> None:
        """Toggle the buttons that would start a competing serial op while detect runs."""
        for btn in (self._btn_detect, self._btn_flash, self._btn_backup, self._btn_erase):
            btn.setEnabled(not busy)

    def _on_detect_failed(self, msg: str) -> None:
        self._log(f"Detection failed: {msg}")
        self._set_detect_busy(False)

    def _on_detect_done(self, result) -> None:
        self._set_detect_busy(False)
        self._log(result.summary)
        if not getattr(result, "is_cyd", False) or not result.variant:
            return
        # Steer the user to the Marauder profile and pre-select the detected variant. The variant list
        # loads async, so remember the key and apply it when the list lands (or immediately if present).
        self._pending_variant = result.variant
        idx = self._profile_combo.findText("Marauder", Qt.MatchContains)
        if idx >= 0 and idx != self._profile_combo.currentIndex():
            self._profile_combo.setCurrentIndex(idx)  # triggers _reload_variants -> applied on load
        elif not self._select_variant(result.variant):
            pass  # not in the current list yet; _on_variants_loaded will add + select it
        if getattr(result, "ambiguous", False) or result.confidence == "low":
            # The panel controller wasn't positively identified (ST7789 fallback bucket), so the exact
            # variant is a guess — don't present it as certain. Pre-select the best guess but warn, and
            # name the alternatives so a blank screen has an obvious next step instead of a dead end.
            self._log(
                f"⚠ Could not positively identify the panel — best guess '{result.variant}'. If the screen "
                "is blank or wrong after flashing, it's likely the wrong panel: try the other ST7789 build "
                "(cyd_2432S028_2usb <-> cyd_2432S024_guition) or the 2.8\" ILI9341 (cyd_2432S028), back up "
                "first, then re-flash."
            )
        else:
            self._log(
                f"Pre-selected variant '{result.variant}'. Pick Marauder firmware and click Flash to "
                "install the correct build."
            )

    def _select_variant(self, key: str) -> bool:
        """Select the variant whose data == key. Returns True if found (and clears the pending key)."""
        for i in range(self._variant_combo.count()):
            if self._variant_combo.itemData(i) == key:
                self._variant_combo.setCurrentIndex(i)
                self._pending_variant = None
                return True
        return False

    # ── Dead Man's Switch toggle ───────────────────────────────────

    def _on_suicide_toggled(self, checked: bool) -> None:
        """Update the card border to orange when the checkbox is active."""
        if checked:
            self._suicide_card.setObjectName("suicide_card_active")
            self._suicide_card.setStyleSheet(
                """
                QFrame#suicide_card_active {
                    background-color: #161b22;
                    border: 2px solid #f0883e;
                    border-radius: 8px;
                    padding: 8px 16px;
                }
                QCheckBox { background: transparent; }
                QLabel { background: transparent; }
                """
            )
        else:
            self._suicide_card.setObjectName("card")
            self._suicide_card.setStyleSheet("")

    @property
    def suicide_enabled(self) -> bool:
        """Whether the Dead Man's Switch checkbox is checked."""
        return self._suicide_checkbox.isChecked()

    @suicide_enabled.setter
    def suicide_enabled(self, value: bool) -> None:
        self._suicide_checkbox.setChecked(value)

    # ── Actions ──────────────────────────────────────────────────────

    def _on_flash(self) -> None:
        # Concurrency guard: refuse a single flash while a single flash OR a batch is already running.
        # _btn_flash is disabled during a flash, but this stops a second _FlashWorker from ever overwriting
        # self._worker (leaking the running thread) and racing the first on the same board. The engine's
        # per-port lock is the last-ditch defense; this stops the double-start at the UI.
        if (self._worker is not None and self._worker.isRunning()) or (
            self._batch_worker is not None and self._batch_worker.isRunning()
        ):
            self._log("A flash is already in progress — wait for it to finish.")
            return
        port = self._port_combo.currentData()
        profile_name = self._profile_combo.currentText()
        if not port:
            self._log("No port selected.")
            return
        profile_path = self._profiles.get(profile_name)
        if not profile_path:
            self._log("No firmware profile selected.")
            return

        # Parse the profile ONCE, up front, guarded — a malformed/browsed file (truncated JSON, a 404
        # HTML page saved as .json, wrong schema) would otherwise raise inside this slot and, with no
        # sys.excepthook installed, PyQt aborts the whole app instead of logging the error.
        try:
            profile = self._fe.load_profile(profile_path)
        except Exception as exc:  # noqa: BLE001
            self._log(f"Invalid firmware profile ({Path(profile_path).name}): {exc}")
            return

        # If Dead Man's Switch is enabled, open setup dialog before flashing
        if self._suicide_checkbox.isChecked():
            try:
                from src.ui.qt.suicide_dialog import SuicideSetupDialog
            except Exception as exc:
                self._log(f"Could not load Dead Man's Switch setup dialog: {exc}")
                return

            dlg = SuicideSetupDialog(self)
            # Pre-populate with current port and profile info
            variant = self._variant_combo.currentData() or ""
            # Set chip from profile if available
            if hasattr(profile, "chip") and profile.chip:
                idx = dlg.chip.findText(profile.chip.lower())
                if idx >= 0:
                    dlg.chip.setCurrentIndex(idx)
            # Set variant from profile if available
            if hasattr(profile, "variant_type") and profile.variant_type:
                idx = dlg.variant.findText(profile.variant_type)
                if idx >= 0:
                    dlg.variant.setCurrentIndex(idx)
            self._log(f"Dead Man's Switch enabled — opening setup for {profile_name} on {port}...")

            result = dlg.exec_()
            if result != SuicideSetupDialog.Accepted:
                self._log("Dead Man's Switch setup cancelled — flash aborted.")
                return
            # FAIL SAFE — do NOT flash plain firmware here. Provisioning wrote a guardcfg bundle to a HOST
            # directory; this GUI flash path does not write that gate to the device (no call to
            # flash_core.flash_suicide anywhere in the UI). Proceeding would flash the firmware with NO boot
            # gate and NO wipe while telling the user the Dead Man's Switch is active — false security on a
            # security tool. Abort with clear next steps (mirrors the TUI's behavior).
            self._log(
                "Dead Man's Switch bundle provisioned on the host, but the GUI cannot yet flash the gate to "
                "the device — aborting so you are NOT left with an unprotected board that looks protected. "
                "Flash the gated bundle with `cyber-controller --deadman-setup`, or uncheck Dead Man's "
                "Switch to flash firmware without the gate."
            )
            return

        variant = self._variant_combo.currentData() or ""
        profile.variant = variant
        # Honor the user-configured Flash Baud Rate (Settings ▸ Flash ▸ flash.flash_baud). Without this the
        # flash always ran at the profile's own baud, so a user LOWERING the baud to make a marginal CH340K /
        # long-cable ESP32 flash reliably had NO effect — the value reached settings.json but no code read it.
        # Falls back to the profile's baud when unset/unparseable, so profiles that pin a baud still win when
        # the user hasn't chosen one.
        try:
            cfg_baud = load_settings().get("flash", {}).get("flash_baud")
        except Exception:  # noqa: BLE001 — a settings read must never block a flash
            cfg_baud = None
        if cfg_baud:
            try:
                profile.baud = int(cfg_baud)
            except (TypeError, ValueError):
                pass
        # Offline vault fallback (read side of the FirmwareVault "offline cache" contract): if this
        # firmware is cached in the vault, hand its path to the engine so a flash with no network can
        # still succeed. The engine uses it ONLY if the live download fails, so the online path keeps
        # its board-aware variant selection.
        try:
            cached = self._vault.get_cached(profile.id)
            if cached:
                profile.offline_fallback_path = str(cached)
        except Exception:  # noqa: BLE001 — a vault lookup must never block a normal flash
            log.debug("vault get_cached lookup failed", exc_info=True)
        if variant:
            self._log(f"Flashing {profile.name} [{variant}] to {port} at {profile.baud} baud...")
        else:
            self._log(f"Flashing {profile.name} to {port} at {profile.baud} baud...")
        # Disable BOTH flash buttons for the duration — a single flash used to leave "Flash Queue" live, so
        # clicking it started a concurrent batch mid-flash. Re-enabled in _on_flash_done.
        self._btn_flash.setEnabled(False)
        self._btn_flash_queue.setEnabled(False)
        self._progress.setValue(0)

        self._worker = _FlashWorker(self._fe, port, profile)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_flash_done)
        self._worker.start()

    def _run_op(self, fn, ok_msg: str, fail_msg: str, buttons) -> None:
        """Run a blocking serial op (backup/erase) on a worker thread so the window stays responsive.
        Re-enables ``buttons`` and logs the outcome on the GUI thread when it finishes."""
        worker = _OpWorker(fn)
        self._op_worker = worker
        self._bg_workers.add(worker)
        worker.progress.connect(self._on_progress)

        def _finish(ok: bool) -> None:
            self._log(ok_msg if ok else fail_msg)
            for b in buttons:
                b.setEnabled(True)

        worker.done.connect(_finish)
        worker.finished.connect(lambda w=worker: self._bg_workers.discard(w))
        worker.start()

    def _on_backup(self) -> None:
        port = self._port_combo.currentData()
        if not port:
            self._log("No port selected.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save backup", f"backup_{port.replace('/', '_')}.bin", "Binary (*.bin)"
        )
        if not path:
            return
        self._log(f"Backing up flash from {port} to {path}...")
        self._btn_backup.setEnabled(False)
        self._run_op(
            lambda cb: self._fe.backup(port, path, progress_callback=cb),
            "Backup complete.", "Backup failed.", (self._btn_backup,),
        )

    def _on_erase(self) -> None:
        port = self._port_combo.currentData()
        if not port:
            self._log("No port selected.")
            return
        from PyQt5.QtWidgets import QMessageBox
        resp = QMessageBox.warning(
            self, "Erase Flash",
            f"This will ERASE ALL flash on the device at {port}.\n\n"
            "This is destructive and cannot be undone. Continue?",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if resp != QMessageBox.Yes:
            self._log("Erase cancelled.")
            return
        self._log(f"Erasing flash on {port}...")
        self._btn_erase.setEnabled(False)
        self._run_op(
            lambda cb: self._fe.erase(port, progress_callback=cb),
            "Erase complete.", "Erase failed.", (self._btn_erase,),
        )

    def _add_to_queue(self) -> None:
        port = self._port_combo.currentData()
        profile_name = self._profile_combo.currentText()
        if port and profile_name:
            item = QListWidgetItem(f"{port} -> {profile_name}")
            item.setData(Qt.UserRole, (port, profile_name))
            self._queue_list.addItem(item)

    def _on_flash_queue(self) -> None:
        """Flash every queued (port, profile) sequentially — the behavior the Batch Queue card and the
        in-app How-To advertise. Reuses the single-flash worker/engine one job at a time (chained on each
        finished signal) so it stays on the exact, proven flash path instead of a second implementation."""
        if self._batch_jobs or (self._batch_worker is not None and self._batch_worker.isRunning()):
            self._log("Batch flash already in progress.")
            return
        # Don't start a batch while a SINGLE flash is running — the reverse (single during batch) is already
        # blocked by disabling _btn_flash at 828-829; this closes the other half of the asymmetry.
        if self._worker is not None and self._worker.isRunning():
            self._log("A single flash is already in progress — wait for it to finish.")
            return
        jobs: list[tuple[str, str]] = []
        for i in range(self._queue_list.count()):
            data = self._queue_list.item(i).data(Qt.UserRole)
            if data:
                jobs.append((data[0], data[1]))
        if not jobs:
            self._log("Batch queue is empty — add port + profile combos first.")
            return
        self._batch_jobs = jobs
        self._batch_idx = 0
        self._batch_ok = 0
        self._btn_flash_queue.setEnabled(False)
        self._btn_flash.setEnabled(False)
        self._log(f"Batch: flashing {len(jobs)} queued device(s) sequentially...")
        self._flash_next_in_batch()

    def _flash_next_in_batch(self) -> None:
        if self._batch_idx >= len(self._batch_jobs):
            total = len(self._batch_jobs)
            self._log(f"Batch complete: {self._batch_ok}/{total} succeeded.")
            self._batch_jobs = []
            self._batch_idx = 0
            self._batch_worker = None
            self._btn_flash_queue.setEnabled(True)
            self._btn_flash.setEnabled(True)
            return
        port, profile_name = self._batch_jobs[self._batch_idx]
        profile_path = self._profiles.get(profile_name)
        if not profile_path:
            self._log(f"Batch [{self._batch_idx + 1}]: no profile '{profile_name}' — skipping.")
            self._advance_batch(False)
            return
        try:
            profile = self._fe.load_profile(profile_path)
        except Exception as exc:  # noqa: BLE001 — a bad profile skips its job, never aborts the batch
            self._log(f"Batch [{self._batch_idx + 1}]: invalid profile {Path(profile_path).name}: {exc}")
            self._advance_batch(False)
            return
        # Honor the user-configured Flash Baud (parity with _on_flash) and the offline vault fallback.
        try:
            cfg_baud = load_settings().get("flash", {}).get("flash_baud")
            if cfg_baud:
                profile.baud = int(cfg_baud)
        except Exception:  # noqa: BLE001
            pass
        try:
            cached = self._vault.get_cached(profile.id)
            if cached:
                profile.offline_fallback_path = str(cached)
        except Exception:  # noqa: BLE001
            log.debug("vault get_cached lookup failed", exc_info=True)
        self._log(f"Batch [{self._batch_idx + 1}/{len(self._batch_jobs)}]: flashing {profile.name} "
                  f"to {port} at {profile.baud} baud...")
        self._batch_worker = _FlashWorker(self._fe, port, profile)
        self._batch_worker.progress.connect(self._on_progress)
        self._batch_worker.finished.connect(self._on_batch_item_done)
        self._batch_worker.start()

    def _on_batch_item_done(self, success: bool) -> None:
        self._log(f"Batch [{self._batch_idx + 1}]: {'succeeded' if success else 'FAILED'}.")
        self._advance_batch(success)

    def _advance_batch(self, success: bool) -> None:
        if success:
            self._batch_ok += 1
        self._batch_idx += 1
        self._flash_next_in_batch()

    # ── Progress / completion ────────────────────────────────────────

    def _on_progress(self, pct: int, msg: str) -> None:
        self._progress.setValue(pct)
        self._log(msg)

    def _on_flash_done(self, success: bool) -> None:
        self._btn_flash.setEnabled(True)
        self._btn_flash_queue.setEnabled(True)
        if success:
            self._progress.setValue(100)
            self._log("Flash completed successfully.")
        else:
            self._log("Flash failed — see log for details.")

    # ── Cleanup ──────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Block until every in-flight background QThread has finished, so app teardown can't destroy a
        still-running QThread ('QThread: Destroyed while thread is still running' -> abort) and a live
        flash / detect / vault-download can't keep touching the board after the GUI is gone. A tab is a
        child widget, so it never gets its own closeEvent when the main window closes — the main window
        calls this from its closeEvent. (closeEvent below covers the popped-out / detached case.)"""
        workers = list(self._bg_workers) + [self._worker, self._detect_worker, self._op_worker]
        for w in workers:
            try:
                if w is not None and w.isRunning():
                    w.wait(3000)
            except RuntimeError:
                # The underlying C++ QThread may already be gone — nothing left to wait on.
                pass

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)

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
            self._vault_status.setObjectName("vault_active")
        else:
            self._vault_status.setText("No cached firmware")
            self._vault_status.setObjectName("muted")

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

        # Run the download on a QThread and marshal every UI update back to the GUI thread via signals.
        # (This used to run on a raw threading.Thread that touched the progress bar / log / status label
        # directly off-thread — undefined behavior in Qt that can corrupt state or segfault the frozen build.)
        worker = _VaultWorker(self._vault, profile_id)
        self._bg_workers.add(worker)
        worker.progress.connect(self._progress.setValue)
        worker.log.connect(self._log)

        def _done(result, name=profile_name) -> None:
            if result:
                self._log(f"Vault: downloaded {name} -> {result}")
            else:
                self._log(f"Vault: download failed for {name}")
            self._refresh_vault_status()

        worker.done.connect(_done)
        worker.finished.connect(lambda w=worker: self._bg_workers.discard(w))
        worker.start()

    def _on_vault_clear(self) -> None:
        """Clear the firmware vault cache."""
        deleted = self._vault.clear_cache()
        self._log(f"Vault: cleared {deleted} cached file(s)")
        self._refresh_vault_status()
