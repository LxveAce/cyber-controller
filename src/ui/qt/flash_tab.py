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


def _uf2_chip_for_variant(
    profile: FirmwareProfile, variant_name: str, loaded_variants: list
) -> str | None:
    """Return the UF2-family chip for a picked variant of a mixed-backend profile, else ``None``.

    Meshtastic's default backend is esptool (the ESP32 family), but it also declares a
    ``chip_uf2_boards`` family — nRF52840 / RP2040 / RP2350 boards that flash by UF2 drag-drop.
    Those chips are NOT auto-detectable (they enumerate as a mass-storage volume, not a serial
    chip-id), so ``profile.chip`` stays ``auto`` and the engine can't tell it should route to the
    uf2 backend. If the picked variant is one of those UF2 boards, return its chip so the caller can
    set ``profile.chip`` and the engine routes correctly. Pure/UI-free so it can be unit-tested; a
    no-op (``None``) for esptool-only profiles or esptool variants, leaving chip auto-detect intact.
    """
    uf2 = (profile.raw.get("resolver_params", {}) or {}).get("chip_uf2_boards", {}) or {}
    fam = uf2.get("boards_by_chip", {}) or {}
    if not fam or not variant_name:
        return None
    for v in loaded_variants:
        if v.get("name") == variant_name and v.get("chip") in fam:
            return v.get("chip")
    return None


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


class _ChipDetectWorker(QThread):
    """Read the esptool chip id off the UI thread — a NON-destructive ``chip_id`` probe (no firmware
    overwrite). Emits ``(port, chip)`` with ``chip=None`` when the board can't be read."""

    done = pyqtSignal(str, object)  # (port, chip: str | None)

    def __init__(self, engine: FlashEngine, port: str) -> None:
        super().__init__()
        self._engine = engine
        self._port = port

    def run(self) -> None:
        try:
            chip = self._engine.detect_chip(self._port)
        except Exception:  # noqa: BLE001 — a probe must never crash the UI thread
            chip = None
        self.done.emit(self._port, chip)


def _format_update_report(updates: list) -> str:
    """Render FirmwareVault.check_updates() output as a log message. Pure/UI-free so it can be
    unit-tested. Each update dict carries name / profile_id / cached_version / latest_version.
    """
    if not updates:
        return "Firmware Vault: all cached firmware is up to date."
    lines = [f"Firmware Vault: {len(updates)} update(s) available —"]
    for u in updates:
        name = u.get("name") or u.get("profile_id") or "?"
        cached = u.get("cached_version", "?")
        latest = u.get("latest_version", "?")
        lines.append(f"  • {name}: cached {cached} → latest {latest}")
    lines.append("Use 'Download to Vault' to fetch the latest.")
    return "\n".join(lines)


class _CheckUpdatesWorker(QThread):
    """Check GitHub for firmware newer than the vault's cache, off the UI thread (network I/O).

    One API call per CACHED profile (FirmwareVault.check_updates). Kept off the GUI thread for the
    same reason as _VaultWorker — network latency must not freeze the UI, and results marshal back
    via signals rather than touching widgets from this thread.
    """

    done = pyqtSignal(list)   # list[dict] from FirmwareVault.check_updates
    failed = pyqtSignal(str)

    def __init__(self, vault) -> None:
        super().__init__()
        self._vault = vault

    def run(self) -> None:
        try:
            self.done.emit(list(self._vault.check_updates()))
        except Exception as exc:  # noqa: BLE001 — a network/vault error must never crash the UI thread
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
        self._batch_jobs: list[tuple[str, str, str]] = []  # (port, profile_name, variant)
        self._batch_idx = 0
        self._batch_ok = 0
        self._pending_variant: str | None = None  # variant to select once the async list lands
        self._pending_flash_hint: str | None = None  # post-flash "screen blank? re-pick" hint (B2)
        # last-fetched variant dicts ({name,label,chip,url}) for the current profile — kept so
        # _on_flash can recover a picked variant's chip (needed to route a mixed-backend profile's
        # UF2 board to the uf2 backend; see _uf2_chip_for_variant).
        self._loaded_variants: list = []
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
        # Read-only chip identify: esptool chip_id (does NOT overwrite firmware). Caches the real chip so
        # the firmware hints + the label stop guessing "unknown" for every classic ESP32 on a shared bridge.
        self._btn_detect_chip = QPushButton("Detect chip")
        self._btn_detect_chip.setToolTip(
            "Read the chip type over serial (esptool chip_id) WITHOUT touching the firmware. Turns the "
            "'unknown chip' guess into a confirmed esp32 / esp32s3 / … so the firmware hints are accurate."
        )
        self._btn_detect_chip.clicked.connect(self._on_detect_chip)
        port_layout.addWidget(self._btn_detect_chip)
        self._chip_label = QLabel("Detected: —")
        self._chip_label.setObjectName("muted")
        port_layout.addWidget(self._chip_label)
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

        self._btn_check_updates = QPushButton("Check for Updates")
        self._btn_check_updates.setToolTip(
            "Check GitHub for firmware releases newer than what you have cached (one API call per "
            "cached profile). Reports what's outdated; does not download anything."
        )
        self._btn_check_updates.clicked.connect(self._on_vault_check_updates)
        vault_row.addWidget(self._btn_check_updates)

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
        prev = self._port_combo.currentData()  # remember the user's selection across the rebuild
        self._port_combo.clear()
        for dev in self._dm.scan_ports():
            self._port_combo.addItem(f"{dev.port} — {dev.name}", dev.port)
        # Empty-state entry (same shape as software_tab's empty drive combo).
        if self._port_combo.count() == 0:
            self._port_combo.addItem("No ports found — plug in a board and press Refresh", None)
        elif prev is not None:
            # Restore the previously-selected port. Without this, Refresh (a natural action after
            # plugging in another board) silently reselects index 0 — a DIFFERENT device — and the
            # next Flash writes to the wrong port. Only fall back to index 0 if that port is gone.
            idx = self._port_combo.findData(prev)
            if idx >= 0:
                self._port_combo.setCurrentIndex(idx)
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
            reg = self._dm.get_device(port)
            if reg is not None and reg.detected_chip:
                chip = reg.detected_chip     # a confirmed esptool chip_id read wins over the USB-VID guess
            else:
                for dev in self._dm.scan_ports():
                    if dev.port == port:
                        chip = _BOARDTYPE_CHIP.get(dev.board_type)
                        break
        # Keep the "Detected:" label in sync with the selected port (its cached/known chip, or — if unread).
        if getattr(self, "_chip_label", None) is not None:
            self._chip_label.setText(f"Detected: {chip}" if chip else "Detected: —")
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
        # (The "Auto" item keeps its honest "default for chip" label; the combo's tooltip already warns it
        # can be wrong for display boards. The blank-CYD risk is surfaced at flash time by the chip-aware
        # B2 gate in _on_flash — see _auto_risks_generic_ili9341 — not by a per-chip guess in the label.)
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
        # Keep the full dicts so _on_flash can recover a picked variant's chip (the combo only
        # stores the variant NAME as data). Needed to route UF2-family boards to the uf2 backend.
        self._loaded_variants = list(variants)
        # Drop the "Loading…" placeholder, keep "Auto" at index 0.
        for i in range(self._variant_combo.count() - 1, 0, -1):
            self._variant_combo.removeItem(i)
        for v in variants:
            label = v.get("label") or v.get("name", "")
            self._variant_combo.addItem(f"{label}  ({v.get('name', '')})", v.get("name", ""))
        # Apply a variant chosen by board-detection now that the real list is in.
        if self._pending_variant:
            self._apply_detected_variant(self._pending_variant)

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
        for btn in (self._btn_detect, self._btn_detect_chip, self._btn_flash, self._btn_backup, self._btn_erase):
            btn.setEnabled(not busy)

    def _on_detect_chip(self) -> None:
        """Read-only chip identify: run esptool chip_id off-thread (no firmware overwrite) and cache the
        real chip so the firmware hints + the 'Detected:' label stop guessing 'unknown' for classic ESP32."""
        port = self._port_combo.currentData()
        if not port:
            self._log("No port selected.")
            return
        self._log(f"Reading chip on {port}…")
        self._set_detect_busy(True)
        self._chip_label.setText("Detected: …")
        self._chip_worker = _ChipDetectWorker(self._fe, port)
        self._chip_worker.done.connect(self._on_chip_detected)
        self._chip_worker.start()

    def _on_chip_detected(self, port: str, chip) -> None:
        self._set_detect_busy(False)
        if chip:
            self._dm.set_detected_chip(port, chip)  # cache on the registry so hints/labels prefer it
            self._log(f"Detected chip on {port}: {chip}")
        else:
            self._log(
                f"Could not read the chip on {port} — the board may be busy, on the wrong port, or not "
                "auto-entering download mode (hold BOOT and retry)."
            )
        # _recolor_profiles is the single source of truth for the firmware hints + the 'Detected:' label
        # (it reads the now-cached chip for the selected port). On a failed read of the CURRENT port with
        # nothing known, surface the honest 'no response' instead of a bare —.
        self._recolor_profiles()
        if chip is None and self._port_combo.currentData() == port and not self._port_chip(port):
            self._chip_label.setText("Detected: (no response)")

    def _on_detect_failed(self, msg: str) -> None:
        self._log(f"Detection failed: {msg}")
        self._set_detect_busy(False)

    def _on_detect_done(self, result) -> None:
        self._set_detect_busy(False)
        self._log(result.summary)
        if not getattr(result, "is_cyd", False) or not result.variant:
            return
        current_name = self._profile_combo.currentText()
        current = self._loaded_or_none(current_name)
        controller = getattr(result, "controller", "") or ""
        # Firmware-aware steering (ledger P-13). Detection identifies the CYD panel; if the user is ALREADY
        # on a profile that supports it (LxveOS carries the 3.5"/2.8" CYDs), keep that profile instead of
        # yanking them to Marauder — the old code always switched, silently dropping their board choice.
        keep_current = (self._profile_supports_controller(current, controller)
                        and not self._is_marauder(current))
        if keep_current:
            # A non-Marauder profile names its builds its own way, so the Marauder variant key wouldn't map
            # to one of its assets (it would fall back to Auto). Keep the profile and point the user at the
            # matching board in THIS profile's picker rather than forcing a key that doesn't fit.
            self._pending_variant = None
            self._log(
                f"Detected {result.label or result.variant}. Your current firmware '{current_name}' "
                "supports this panel — pick the matching board under 'Board / variant', then Flash."
            )
        else:
            # Marauder already current -> apply its detected key now; or the current profile can't flash a
            # display -> steer to Marauder (the display default) and pre-select the variant. The list loads
            # async, so remember the key and apply it when the list lands (or immediately if present).
            self._pending_variant = result.variant
            idx = self._profile_combo.findText("Marauder", Qt.MatchContains)
            if idx >= 0 and idx != self._profile_combo.currentIndex():
                self._profile_combo.setCurrentIndex(idx)  # triggers _reload_variants -> applied on load
            elif idx >= 0:
                # Marauder is ALREADY current, so the switch is skipped and no variant reload fires --
                # _on_variants_loaded (which applies a pending key) never runs. Apply the detected key
                # here, or the picker silently stays on Auto and Flash writes the generic ILI9341
                # default over the panel detection just identified.
                self._apply_detected_variant(result.variant)
            # else: no Marauder profile present -- keep _pending_variant for whenever its list loads.
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
        elif not keep_current:
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

    def _apply_detected_variant(self, key: str) -> None:
        """Select a board-detection variant KEY, adding it as a selectable synthetic '(detected)'
        item when the loaded list doesn't carry it. The combo stores each item's asset NAME as data,
        which never equals a bare detection key ('cyd_2432S028_2usb' vs a '...cyd_2432S028_2usb...'
        asset name), so _select_variant alone can't match a detect key against a freshly loaded list
        -- the synthetic item is what makes the detected panel actually reach Flash, not Auto."""
        if not self._select_variant(key):
            self._variant_combo.addItem(f"{key}  (detected)", key)
            self._select_variant(key)

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

    @staticmethod
    def _is_marauder(profile) -> bool:
        """True for the ESP32 Marauder profile. ``str()`` guards a browsed profile whose ``id``/``protocol``
        isn't a string (e.g. ``"id": 7``) — that would otherwise raise ``AttributeError`` inside this Qt
        slot and, with no ``sys.excepthook`` installed, abort the whole app."""
        return str(getattr(profile, "id", "") or "").lower() == "marauder" or \
               str(getattr(profile, "protocol", "") or "").lower() == "marauder"

    @staticmethod
    def _profile_supports_controller(profile, controller: str) -> bool:
        """True when *profile* lists a display board whose panel controller matches the detected one.

        LxveOS carries an ``st7796`` board (3.5" CYD) and an ``ili9341`` one (2.8").
        This lets 'Detect' keep a display profile the user already picked instead of
        yanking them to Marauder, which silently dropped a LxveOS panel choice (P-13).
        Reads the same ``boards`` metadata as ``chip_match``. Pure, so it's unit-testable.
        """
        want = str(controller or "").strip().lower()
        if profile is None or not want:
            return False
        for b in getattr(profile, "boards", None) or []:
            if not isinstance(b, dict) or not b.get("has_display"):
                continue
            if str(b.get("display_type") or "").strip().lower() == want:
                return True
        return False

    def _port_chip(self, port) -> str | None:
        """The chip of the board on ``port`` IF unambiguously known from USB enumeration (native-USB
        S3/S2/C3) OR confirmed by a read-only "Detect chip" (esptool chip_id, cached on the registry Device).
        ``None`` for the classic-ESP32 / shared-UART-bridge bucket that hasn't been probed — which includes
        every un-probed CYD, and is exactly where Marauder's Auto default (the generic ILI9341
        ``old_hardware`` build) can blank a display. Mirrors the chip lookup in :meth:`_recolor_profiles`."""
        if not port:
            return None
        reg = self._dm.get_device(port)
        if reg is not None and reg.detected_chip:
            return reg.detected_chip   # a confirmed chip_id read wins over the USB-VID guess
        for dev in self._dm.scan_ports():
            if dev.port == port:
                return _BOARDTYPE_CHIP.get(dev.board_type)
        return None

    def _loaded_or_none(self, profile_name: str):
        """Load a profile by display name, or ``None`` if it's missing/unparseable (never raises)."""
        path = self._profiles.get(profile_name)
        if not path:
            return None
        try:
            return self._fe.load_profile(path)
        except Exception:  # noqa: BLE001 — a bad profile is a no-risk None, never a crash
            return None

    def _auto_risks_generic_ili9341(self, profile, variant: str, port) -> bool:
        """True when a flash would fall to Marauder's classic-esp32 Auto default (the generic ILI9341
        ``old_hardware`` build) on a board we CANNOT positively identify as a non-classic chip — i.e. the
        one case that blanks most CYD panels. Marauder's Auto is per-chip (esp32 -> old_hardware; esp32s3
        -> multiboardS3; esp32s2 -> flipper; esp32c5 -> its devkit build), so when the chip is known to be
        S3/S2/C3 Auto resolves to the CORRECT build and there is nothing to warn about."""
        return variant == "" and self._is_marauder(profile) and self._port_chip(port) is None

    @staticmethod
    def _generic_ili9341_message(
        profile_name: str, count: int, picker_visible: bool
    ) -> "tuple[str, str]":
        """Build the (title, body) for the generic-ILI9341 Auto-flash confirm. Pure + parameterised
        so the copy is testable without a modal dialog. 'Detect board (CYD)' auto-identifies the
        panel, so it is the RECOMMENDED first step for the "not sure which CYD this is" case,
        not a shortcut only for users who already know their board (the old phrasing undersold it).
        When the picker + Detect are hidden (Simple mode) point at Pro mode instead. Phrased
        conditionally because we can't know the resolved build until flash time."""
        if picker_visible:
            how = ("click 'Detect board (CYD)' to identify the panel automatically, or pick it "
                   "under 'Board / variant', first")
        else:
            how = ("switch to Pro mode (Ctrl+M) to run 'Detect board (CYD)' or pick your exact "
                   "board first")
        who = f"{count} queued Marauder job(s)" if count > 1 else f"'{profile_name}'"
        body = (
            f"{who} will flash with Auto. This board's chip isn't uniquely identified over USB, "
            "so if it's a classic-ESP32 display board (a Cheap-Yellow-Display, M5, etc.) Auto "
            "flashes the generic ILI9341 build — the wrong display driver for most such panels, "
            "leaving the screen blank.\n\n"
            f"Recommended: {how}. Or flash the per-chip default anyway."
        )
        return "Flash the per-chip default build?", body

    def _confirm_generic_ili9341(self, profile_name: str, *, count: int = 1) -> bool:
        """Honest, mode-aware confirm for a Marauder Auto flash on an unidentified (classic-esp32)
        board. Returns True to proceed. The remediation names controls that exist in the current
        mode — Simple mode hides the picker + Detect, so it points at Ctrl+M / Pro instead."""
        from PyQt5.QtWidgets import QMessageBox

        title, text = self._generic_ili9341_message(
            profile_name, count, self._variant_combo.isVisibleTo(self)
        )
        resp = QMessageBox.warning(
            self, title, text,
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        return resp == QMessageBox.Yes

    def _release_port_for_flash(self, port: str) -> None:
        """Free the exclusive COM port before esptool opens it. If the Devices tab (or any panel) holds
        a serial monitor open on this port via the DeviceManager, esptool's second exclusive open raises
        'Access is denied' on Windows and the flash never starts. A no-owner release force-closes it
        regardless of owner; it is a safe no-op when nothing is connected on *port*."""
        try:
            self._dm.close_connection(port)
        except Exception:  # noqa: BLE001 — releasing a port must never block a flash
            log.debug("pre-flash port release failed for %s", port, exc_info=True)

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
        # If the picked variant is a UF2 board of a mixed-backend profile (Meshtastic nRF52840/
        # RP2040/RP2350), set profile.chip to that board's chip so the engine routes it to the uf2
        # backend instead of esptool — those chips aren't auto-detectable, so without this the flash
        # falls through to esptool and fails on a .uf2. No-op (auto-detect intact) for esptool.
        uf2_chip = _uf2_chip_for_variant(profile, variant, self._loaded_variants)
        if uf2_chip:
            profile.chip = uf2_chip
        # B2 — flash-default honesty. Marauder's Auto default is per-chip; on the classic-esp32 bucket
        # (every CYD, which we can't positively ID over a shared UART bridge) it's the generic ILI9341
        # build, the wrong display driver for most such panels — their screen stays blank after a flash
        # that reports success. Warn ONLY on that ambiguous case (a known S3/S2/C3 flashes its correct
        # build with no nag), and phrase it conditionally. Cancel lets the user pick their panel first.
        self._pending_flash_hint = None
        if self._auto_risks_generic_ili9341(profile, variant, port):
            if not self._confirm_generic_ili9341(profile.name):
                self._log("Flash cancelled — pick your panel first (Board / variant, or Detect board), "
                          "then flash.")
                return
            self._log("Flashing Marauder's per-chip Auto default at your confirmation "
                      "(the generic ILI9341 build on a classic-ESP32 board).")
            self._pending_flash_hint = (
                "If the screen is blank or wrong, this board took the generic ILI9341 build — pick your "
                "exact panel under 'Board / variant' (or run Detect board) and re-flash."
            )
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

        self._release_port_for_flash(port)  # release an open monitor so esptool can claim the port
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
            # Capture the board/variant selection WITH the job so a queued flash uses the panel the user
            # picked — the queue used to store only (port, profile) and silently drop the variant, so a
            # batch flash always fell to the per-chip default (a generic ILI9341 blank screen on a CYD).
            variant = self._variant_combo.currentData() or ""
            label = f"{port} -> {profile_name}" + (f"  [{variant}]" if variant else "")
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, (port, profile_name, variant))
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
        jobs: list[tuple[str, str, str]] = []
        for i in range(self._queue_list.count()):
            data = self._queue_list.item(i).data(Qt.UserRole)
            if data:
                # Back-compat: older/2-tuple items carry no variant.
                jobs.append((data[0], data[1], data[2] if len(data) > 2 else ""))
        if not jobs:
            self._log("Batch queue is empty — add port + profile combos first.")
            return
        # B2 — same honest gate as a single flash, but ONCE up front (a modal mid-batch would stall the
        # run). If any queued job is a Marauder Auto flash on a board we can't positively identify, confirm
        # before starting the whole batch instead of silently flashing generic-ILI9341 to a CYD.
        at_risk = sum(1 for (p, name, v) in jobs
                      if self._auto_risks_generic_ili9341(self._loaded_or_none(name), v, p))
        if at_risk and not self._confirm_generic_ili9341("", count=at_risk):
            self._log("Batch cancelled — pick a panel for the Marauder job(s), then flash the queue.")
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
        port, profile_name, variant = self._batch_jobs[self._batch_idx]
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
        # Flash the board/variant that was queued with this job (parity with _on_flash) — not the per-chip
        # default. This is what makes a queued CYD flash its real panel instead of the generic ILI9341 build.
        profile.variant = variant
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
        self._release_port_for_flash(port)  # release an open monitor so esptool can claim the port
        self._batch_worker = _FlashWorker(self._fe, port, profile)
        self._batch_worker.progress.connect(self._on_progress)
        self._batch_worker.finished.connect(self._on_batch_item_done)
        self._batch_worker.start()

    def _on_batch_item_done(self, success: bool) -> None:
        self._log(f"Batch [{self._batch_idx + 1}]: {'succeeded' if success else 'FAILED'}.")
        # B2 — if this job took Marauder's generic-ILI9341 Auto default (a display board we couldn't ID),
        # restate the guess so a blank CYD reads as "wrong build, re-pick" instead of a green success.
        if success and self._batch_idx < len(self._batch_jobs):
            p, name, v = self._batch_jobs[self._batch_idx]
            if self._auto_risks_generic_ili9341(self._loaded_or_none(name), v, p):
                self._log("  ↳ if this board's screen is blank, it took the generic ILI9341 build — "
                          "re-queue it with your exact panel picked.")
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
            # B2 — after a generic-default Marauder flash, restate that the display build was a guess so a
            # blank screen reads as "wrong build, re-pick" instead of a clean success with a dead screen.
            if self._pending_flash_hint:
                self._log(self._pending_flash_hint)
        else:
            self._log("Flash failed — see log for details.")
        self._pending_flash_hint = None

    # ── Cleanup ──────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Block until every in-flight background QThread has finished, so app teardown can't destroy a
        still-running QThread ('QThread: Destroyed while thread is still running' -> abort) and a live
        flash / detect / vault-download can't keep touching the board after the GUI is gone. A tab is a
        child widget, so it never gets its own closeEvent when the main window closes — the main window
        calls this from its closeEvent. (closeEvent below covers the popped-out / detached case.)"""
        # _batch_worker MUST be joined too: a sequential Flash Queue runs each item on it, and closing
        # the window mid-batch would otherwise destroy a still-running QThread mid esptool-write (a
        # half-written flash bricks the board) or fire its finished/progress callbacks into already-
        # deleted widgets. It is not in _bg_workers, so list it explicitly like the other singletons.
        workers = list(self._bg_workers) + [
            self._worker, self._batch_worker, self._detect_worker, self._op_worker]
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
        # Mirror to the app-wide activity bus so the persistent terminal reflects flashing, backup,
        # erase, detect and vault output — every esptool/tool subprocess line already arrives here as
        # `msg` (flash_engine._percent_adapter forwards them). Guarded so logging can never break a flash.
        try:
            from src.core.activity_log import activity_log
            activity_log().emit_line("flash", msg)
        except Exception:  # noqa: BLE001
            pass

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

    def _on_vault_check_updates(self) -> None:
        """Check GitHub for firmware newer than the vault cache (off-thread); report to the log."""
        self._btn_check_updates.setEnabled(False)
        self._log("Firmware Vault: checking GitHub for updates to cached firmware…")
        worker = _CheckUpdatesWorker(self._vault)
        self._bg_workers.add(worker)
        worker.done.connect(self._on_check_updates_done)
        worker.failed.connect(self._on_check_updates_failed)
        worker.finished.connect(lambda w=worker: self._bg_workers.discard(w))
        worker.start()

    def _on_check_updates_done(self, updates: list) -> None:
        self._log(_format_update_report(updates))
        self._btn_check_updates.setEnabled(True)

    def _on_check_updates_failed(self, msg: str) -> None:
        self._log(f"Firmware Vault: update check failed — {msg}")
        self._btn_check_updates.setEnabled(True)

    def _on_vault_clear(self) -> None:
        """Clear the firmware vault cache."""
        deleted = self._vault.clear_cache()
        self._log(f"Vault: cleared {deleted} cached file(s)")
        self._refresh_vault_status()
