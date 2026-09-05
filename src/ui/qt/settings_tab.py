"""Settings tab — edit persistent application configuration.

Backed by :mod:`src.config.settings`.  Groups settings into Serial, Flash,
Cross-Comm, and Firmware Vault sections.  Save writes to disk; Reset restores
the in-memory defaults (and the user can then Save to persist them).
"""

from __future__ import annotations

import logging

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import DEFAULTS, load_settings, save_settings

log = logging.getLogger(__name__)

#: Flash-baud combo label for "use the firmware profile's own baud" (persisted as None, not an integer).
_FLASH_BAUD_AUTO = "Auto (firmware default)"


def format_update_status(current: str, updates: dict) -> str:
    """One human line describing the update state — so the operator can SEE the check runs, not just
    trust it (the owner's 'idk if it's working'). Pure + Qt-free so it's unit-testable. Shows the
    running version, when it last checked, and whether the newest release seen is ahead."""
    parts = [f"Current: v{current}"]
    last = str((updates or {}).get("last_check_iso") or "").strip()
    parts.append(f"last checked {last.replace('T', ' ')}" if last else "not checked yet")
    seen = str((updates or {}).get("last_seen_latest") or "").strip()
    if seen:
        from src.core import install
        if install._parse(seen) > install._parse(current):
            parts.append(f"newer release available: {seen}")
        else:
            parts.append("up to date")
    return "   ·   ".join(parts)


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


class SettingsTab(QWidget):
    """Editor for persistent application settings.

    Reads via :func:`load_settings`, writes via :func:`save_settings`.
    Reloads from disk each time the tab is shown so it never displays stale
    values after another component changed the file.
    """

    #: Emitted when the user clicks "Check now" — the main window runs a manual (forced) update check.
    check_updates_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._last_settings_size: "str | None" = None   # last size class (debounce)
        self._settings = load_settings()
        self._dirty = False    # True once the user edits a field; blocks showEvent's disk-reload from
        self._loading = False  # clobbering it. _loading guards programmatic _load_into_ui from self-marking.
        self._build_ui()
        self._connect_signals()
        self._load_into_ui(self._settings)
        self._refresh_gate_status()

    # ── Layout ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        root = QVBoxLayout(container)

        # Cards flow into a 1/2/3-column grid (see _apply_settings_layout).
        # Each goes into self._cards and is placed by the resolver rather than stacked directly.
        self._cards: list[QFrame] = []
        self._settings_muted_labels: list[QLabel] = []
        self._suppressed_cards: set = set()   # cards Simple mode hides — kept out of the grid flow
        self._cards_grid = QGridLayout()
        root.addLayout(self._cards_grid)

        # ── Serial ───────────────────────────────────────────────────
        serial_card, serial_outer = _make_card("Serial Defaults")
        serial_form = QFormLayout()
        serial_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        # Keep the field at its natural width (a baud value is 6 digits) instead of stretching it
        # across the whole card with the value hugging the left edge.
        serial_form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        self._baud_combo = QComboBox()
        self._baud_combo.setEditable(True)
        self._baud_combo.setMinimumWidth(120)
        self._baud_combo.addItems(["9600", "57600", "115200", "230400", "460800", "921600"])
        serial_form.addRow("Default Baud Rate:", self._baud_combo)
        serial_outer.addLayout(serial_form)
        self._cards.append(serial_card)

        # ── Flash ────────────────────────────────────────────────────
        flash_card, flash_outer = _make_card("Flash Defaults")
        flash_form = QFormLayout()
        flash_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        flash_form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        self._flash_baud_combo = QComboBox()
        self._flash_baud_combo.setEditable(True)
        self._flash_baud_combo.setMinimumWidth(120)
        # "Auto" (the default) uses each firmware's own baud; a number is an explicit override.
        self._flash_baud_combo.addItems([_FLASH_BAUD_AUTO, "115200", "230400", "460800", "921600"])
        flash_form.addRow("Flash Baud Rate:", self._flash_baud_combo)
        flash_outer.addLayout(flash_form)
        self._cards.append(flash_card)

        # ── Interface ────────────────────────────────────────────────
        iface_card, iface_outer = _make_card("Interface")
        iface_form = QFormLayout()
        iface_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self._touch_mode_combo = QComboBox()
        self._touch_mode_combo.addItem("Auto (detect a touchscreen)", "auto")
        self._touch_mode_combo.addItem("On (force touch-sized controls)", "on")
        self._touch_mode_combo.addItem("Off (force pointer-sized controls)", "off")
        self._touch_mode_combo.setToolTip(
            "Touch mode sizes the responsive layout — bigger hit targets and the touch stacking on "
            "the Nodes/Crack screens. Auto detects a touch device; force it On for a touchscreen "
            "deck where detection is unreliable. Takes full effect on the next window resize or "
            "restart. (Auto-detection is not yet validated on real touch hardware.)")
        iface_form.addRow("Touch mode:", self._touch_mode_combo)
        iface_outer.addLayout(iface_form)
        self._cards.append(iface_card)

        # ── Cross-Comm ───────────────────────────────────────────────
        # The old "Auto-share discoveries" / "De-duplicate targets by MAC" checkboxes were inert — nothing
        # read them, so unchecking either changed nothing. Rather than ship toggles that lie, describe the
        # real, always-on behavior honestly (removal mirrors how the inert serial.timeout / flash.* controls
        # were handled). De-dup by MAC is intrinsic to the pool: targets are keyed by type:mac.
        self._comm_card, comm_outer = _make_card("Cross-Communication")
        comm_card = self._comm_card
        comm_desc = QLabel(
            "Every device's scan discoveries are shared into one target pool and de-duplicated by MAC, so "
            "an AP found by one radio is actionable from another. This is always on — there is nothing to "
            "configure here."
        )
        comm_desc.setObjectName("muted")
        comm_desc.setWordWrap(True)
        self._settings_muted_labels.append(comm_desc)
        comm_outer.addWidget(comm_desc)
        self._cards.append(comm_card)

        # ── Updates ──────────────────────────────────────────────────
        self._updates_card, updates_outer = _make_card("Updates")
        updates_card = self._updates_card
        updates_desc = QLabel(
            "On launch, quietly check GitHub for a newer release. On a packaged build you can install it "
            "in place — the download is SHA-256 verified, then the app swaps itself and restarts; from a "
            "source checkout it links you to the release page instead. You choose when to update."
        )
        updates_desc.setObjectName("muted")
        updates_desc.setWordWrap(True)
        self._settings_muted_labels.append(updates_desc)
        updates_outer.addWidget(updates_desc)
        self._updates_enabled_check = QCheckBox("Automatically check for updates")
        updates_outer.addWidget(self._updates_enabled_check)
        # A live status line so the check is visible, not just trusted (owner: "idk if it's working").
        self._update_status_lbl = QLabel("")
        self._update_status_lbl.setObjectName("muted")
        self._update_status_lbl.setWordWrap(True)
        updates_outer.addWidget(self._update_status_lbl)
        updates_btn_row = QHBoxLayout()
        updates_btn_row.addStretch()
        self._check_updates_btn = QPushButton("Check now")
        updates_btn_row.addWidget(self._check_updates_btn)
        updates_outer.addLayout(updates_btn_row)
        self._cards.append(updates_card)

        # ── Wardrive uploads (WiGLE) ──────────────────────────
        self._uploads_card, uploads_outer = _make_card("Wardrive uploads (WiGLE)")
        uploads_card = self._uploads_card
        uploads_desc = QLabel(
            "Add a wardriving-service token to upload a wardrive CSV straight from the Wardrive tab when the "
            "machine has internet. Leave a field blank to keep that service off. Tokens are credentials — "
            "they're stored locally in your settings.\n"
            "• WiGLE: the “Encoded for use” token from wigle.net ▸ Account.\n"
            "• WDG Wars: the 64-hex API key from wdgwars.pl ▸ Profile."
        )
        uploads_desc.setObjectName("muted")
        uploads_desc.setWordWrap(True)
        self._settings_muted_labels.append(uploads_desc)
        uploads_outer.addWidget(uploads_desc)
        token_form = QFormLayout()
        token_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self._wigle_token_edit = QLineEdit()
        self._wigle_token_edit.setEchoMode(QLineEdit.Password)   # a credential — mask it
        self._wigle_token_edit.setPlaceholderText("WiGLE Encoded-for-use token")
        token_form.addRow("WiGLE token:", self._wigle_token_edit)
        self._wdgwars_token_edit = QLineEdit()
        self._wdgwars_token_edit.setEchoMode(QLineEdit.Password)
        self._wdgwars_token_edit.setPlaceholderText("WDG Wars API key (64 hex)")
        token_form.addRow("WDG Wars token:", self._wdgwars_token_edit)
        uploads_outer.addLayout(token_form)
        self._cards.append(uploads_card)

        # ── Safety & Disclaimers ─────────────────────────────────────
        # These LABEL and warn; they never remove or block a capability. The
        # confirm dialog always offers "Yes, proceed"; suppress turns it off.
        self._safety_card, safety_outer = _make_card("Safety & Disclaimers")
        safety_card = self._safety_card
        safety_form = QFormLayout()
        safety_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self._confirm_dangerous_check = QCheckBox(
            "Confirm before sending dangerous commands (deauth / jam / beacon spam / ...)"
        )
        self._suppress_warnings_check = QCheckBox(
            "Suppress all safety warnings (controlled-lab use — you remain responsible)"
        )
        safety_form.addRow(self._confirm_dangerous_check)
        safety_form.addRow(self._suppress_warnings_check)
        safety_outer.addLayout(safety_form)
        self._cards.append(safety_card)

        # ── Access Gate (Security) ───────────────────────────────────
        self._gate_card, gate_outer = _make_card("Access Gate (Security)")
        gate_card = self._gate_card
        gate_desc = QLabel(
            "Lock the app behind an admin password and/or a physical USB key. Secrets are stored as "
            "salted hashes (no plaintext); protected data stays encrypted until unlocked. Applies on "
            "the next launch."
        )
        gate_desc.setObjectName("muted")
        gate_desc.setWordWrap(True)
        self._settings_muted_labels.append(gate_desc)
        gate_outer.addWidget(gate_desc)
        self._gate_status_lbl = QLabel("")
        self._gate_status_lbl.setObjectName("muted")
        self._gate_status_lbl.setWordWrap(True)
        gate_outer.addWidget(self._gate_status_lbl)
        self._gate_setup_btn = QPushButton("Set up access gate (password / key)…")
        self._gate_setup_btn.setToolTip("Set or change the admin password, create a physical USB key, "
                                        "or choose the unlock policy — from inside the app.")
        gate_outer.addWidget(self._gate_setup_btn)
        self._cards.append(gate_card)

        # ── Secure Container (Security) ──────────────────────────────
        # When ON, saved macros are encrypted at rest in a gate-keyed AES-256-GCM container and are
        # unreadable while the access gate is locked. Off by default. Only recorded macros currently write
        # through the container (logs are in-memory/session-only; CSV exports are plaintext by design), so
        # the copy names macros specifically instead of over-promising logs/sessions/captures encryption.
        self._secure_card, secure_outer = _make_card("Secure Container (Security)")
        secure_card = self._secure_card
        secure_desc = QLabel(
            "Encrypt your saved macros at rest in a gate-keyed container (~/.cyber-controller/secure, "
            "AES-256-GCM). The container is sealed — unreadable — whenever the access gate is locked, and "
            "the encryption key lives only inside the unlocked vault (never in the clear). Logs are kept in "
            "memory for the session and aren't written to disk; explicit exports you choose to share (e.g. a "
            "WiGLE wardrive CSV) stay plaintext by design."
        )
        secure_desc.setObjectName("muted")
        secure_desc.setWordWrap(True)
        self._settings_muted_labels.append(secure_desc)
        secure_outer.addWidget(secure_desc)
        self._secure_container_check = QCheckBox("Encrypt my saved macros in the secure container")
        secure_outer.addWidget(self._secure_container_check)
        self._cards.append(secure_card)

        # ── Firmware Vault ───────────────────────────────────────────
        self._vault_card, vault_outer = _make_card("Firmware Vault")
        vault_card = self._vault_card
        vault_form = QFormLayout()
        vault_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        dir_row = QHBoxLayout()
        self._vault_dir_edit = QLineEdit()
        self._vault_dir_edit.setPlaceholderText("~/.cyber-controller/firmware_vault")
        self._vault_dir_edit.setMinimumWidth(150)
        self._vault_browse_btn = QPushButton("Browse...")
        dir_row.addWidget(self._vault_dir_edit, stretch=1)
        dir_row.addWidget(self._vault_browse_btn)
        vault_form.addRow("Vault Directory:", dir_row)
        vault_outer.addLayout(vault_form)
        self._cards.append(vault_card)

        # ── Save / Reset ─────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._reset_btn = QPushButton("Reset to Defaults")
        self._save_btn = QPushButton("Save Settings")
        self._save_btn.setObjectName("btnGreen")  # reform green go-action (flash_btn is flattened under primer)
        btn_row.addWidget(self._reset_btn)
        btn_row.addWidget(self._save_btn)
        root.addLayout(btn_row)

        root.addStretch()

        scroll.setWidget(container)
        outer.addWidget(scroll)
        from src.ui.qt import primer
        primer.apply_primer(self)   # mockup formula: tight fields / .btn buttons across every settings card
        self._relayout_settings(force=True)   # initial placement of the cards into the grid

    # ── Responsive layout ───────────────────────────

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._relayout_settings()

    def _relayout_settings(self, force: bool = False) -> None:
        """Re-flow the cards when the size class changes (debounced). ``force`` re-applies even when
        the class is unchanged — used after set_ui_mode() toggles card visibility so the grid
        compacts around the now-hidden cards."""
        from src.ui.qt.layout_profile import layout_profile, settings_layout
        dpi = self.logicalDpiX() or 96
        from src.ui.qt.touch_mode import touch_active
        profile = layout_profile(max(1, self.width()), max(1, self.height()),
                                 touch=touch_active(), dpi=dpi)
        if not force and profile.size == self._last_settings_size:   # debounce on the size class
            return
        self._last_settings_size = profile.size
        self._apply_settings_layout(settings_layout(profile))

    def _apply_settings_layout(self, sl) -> None:
        """Place VISIBLE cards row-major into an sl.columns-wide grid; dense chrome tightens grid
        spacing and demotes the long helper-text descriptions (never a functional control)."""
        cols = max(1, sl.columns)
        grid = self._cards_grid
        for card in self._cards:
            grid.removeWidget(card)
        visible = [c for c in self._cards if c not in self._suppressed_cards]
        for i, card in enumerate(visible):
            grid.addWidget(card, i // cols, i % cols)
        for col in range(3):
            grid.setColumnStretch(col, 1 if col < cols else 0)
        grid.setHorizontalSpacing(8 if sl.collapse_chrome else 16)
        grid.setVerticalSpacing(8 if sl.collapse_chrome else 12)
        for lbl in self._settings_muted_labels:
            lbl.setVisible(not sl.collapse_chrome)

    # ── Dual-depth (Simple / Pro) ────────────────────────────────────

    def set_ui_mode(self, mode: str) -> None:
        """Simple shows only Serial + Flash defaults; Pro shows every section. Advanced sections
        (Cross-Comm, Safety & Disclaimers, Access Gate, Secure Container, Firmware Vault) are hidden
        in Simple — their saved values are untouched, just not editable in the streamlined view."""
        pro = str(mode).lower() != "simple"
        for card in (
            getattr(self, "_comm_card", None), getattr(self, "_safety_card", None),
            getattr(self, "_gate_card", None), getattr(self, "_secure_card", None),
            getattr(self, "_vault_card", None), getattr(self, "_uploads_card", None),
        ):
            if card is not None:
                card.setVisible(pro)
                # Track the suppressed set so the re-flow keeps them out of the flow (Simple) or
                # restores them (Pro) — independent of Qt's pre-show isHidden() state.
                if pro:
                    self._suppressed_cards.discard(card)
                else:
                    self._suppressed_cards.add(card)
        # Re-flow the grid so it compacts around the cards Simple mode just hid (force: the size
        # class hasn't changed, but the visible-card set has).
        self._relayout_settings(force=True)

    def _connect_signals(self) -> None:
        self._save_btn.clicked.connect(self._on_save)
        self._reset_btn.clicked.connect(self._on_reset)
        self._vault_browse_btn.clicked.connect(self._on_browse_vault)
        self._suppress_warnings_check.toggled.connect(self._on_suppress_toggled)
        self._secure_container_check.toggled.connect(self._on_secure_container_toggled)
        self._gate_setup_btn.clicked.connect(self._on_gate_setup)
        self._check_updates_btn.clicked.connect(self.check_updates_requested)
        # Mark the tab dirty on any user edit so showEvent's disk-reload won't silently discard it. These
        # fire on programmatic setText/setChecked too, but _mark_dirty ignores those (the _loading guard).
        self._baud_combo.currentTextChanged.connect(self._mark_dirty)
        self._flash_baud_combo.currentTextChanged.connect(self._mark_dirty)
        self._vault_dir_edit.textChanged.connect(self._mark_dirty)
        self._confirm_dangerous_check.toggled.connect(self._mark_dirty)
        self._updates_enabled_check.toggled.connect(self._mark_dirty)
        self._suppress_warnings_check.toggled.connect(self._mark_dirty)
        self._secure_container_check.toggled.connect(self._mark_dirty)
        self._wigle_token_edit.textChanged.connect(self._mark_dirty)
        self._wdgwars_token_edit.textChanged.connect(self._mark_dirty)
        self._touch_mode_combo.currentIndexChanged.connect(self._on_touch_mode_changed)

    def _mark_dirty(self, *_args) -> None:
        if not self._loading:
            self._dirty = True

    def _on_touch_mode_changed(self, *_args) -> None:
        """Apply the touch-mode override live (so the next relayout uses it) and mark the tab dirty
        so the choice persists on Save. Applied even during load — that just syncs the module global
        to the value read from disk, which is correct."""
        from src.ui.qt.touch_mode import set_touch_mode
        set_touch_mode(self._touch_mode_combo.currentData())
        self._mark_dirty()

    # ── Load / gather ────────────────────────────────────────────────

    def _load_into_ui(self, settings: dict) -> None:
        """Populate widgets from a settings dict. Sets _loading so the change signals wired in
        _connect_signals don't mistake this programmatic repopulation for a user edit."""
        self._loading = True
        try:
            self._load_into_ui_inner(settings)
        finally:
            self._loading = False

    def _load_into_ui_inner(self, settings: dict) -> None:
        serial = settings.get("serial", {})
        self._set_combo_text(self._baud_combo, str(serial.get("default_baud", 115200)))

        flash = settings.get("flash", {})
        fb = flash.get("flash_baud")
        # None/unset -> "Auto" (firmware profile's own baud); a concrete int -> that explicit override.
        self._set_combo_text(self._flash_baud_combo, _FLASH_BAUD_AUTO if not fb else str(fb))

        interface = settings.get("interface", {})
        tm_idx = self._touch_mode_combo.findData(str(interface.get("touch_mode", "auto")))
        self._touch_mode_combo.setCurrentIndex(tm_idx if tm_idx >= 0 else 0)

        sec = settings.get("safety", {})
        self._confirm_dangerous_check.setChecked(bool(sec.get("confirm_dangerous", True)))
        # Set the suppress box WITHOUT triggering its acknowledgement dialog.
        self._suppress_warnings_check.blockSignals(True)
        self._suppress_warnings_check.setChecked(bool(sec.get("suppress_all_warnings", False)))
        self._suppress_warnings_check.blockSignals(False)

        security = settings.get("security", {})
        self._secure_container_check.blockSignals(True)
        self._secure_container_check.setChecked(bool(security.get("secure_container", False)))
        self._secure_container_check.blockSignals(False)

        vault = settings.get("vault", {})
        self._vault_dir_edit.setText(str(vault.get("dir", "")))

        updates = settings.get("updates", {})
        self._updates_enabled_check.setChecked(bool(updates.get("enabled", True)))
        self._refresh_update_status(updates)

        uploads = settings.get("uploads", {})
        self._wigle_token_edit.setText(str(uploads.get("wigle_token", "")))
        self._wdgwars_token_edit.setText(str(uploads.get("wdgwars_token", "")))

    def _gather(self) -> dict:
        """Read the current UI state into a settings dict.

        Carry-forward sections this tab owns no widgets for (interface / updates bookkeeping / the
        one-time acks) are read FRESH from disk here, not from the long-lived self._settings snapshot
        taken on show. Another in-process flow (a 'Check now' that suppresses an update prompt, a
        Ctrl+M interface-mode / loadout change) can write settings.json AFTER this tab was shown — a
        modal over the already-visible tab fires no showEvent, so the snapshot goes stale. Overlaying
        only the widget-backed keys onto the current on-disk state stops a plain Save from silently
        reverting that concurrent write.
        """
        disk = load_settings()
        return {
            # Only widget-backed keys are gathered; the other keys DEFAULTS carries for these sections
            # (serial.timeout, flash.verify/auto_backup/mode, cross_comm.auto_share/dedup_by_mac) had no
            # consumer in the Qt app, so their inert controls were removed — save_settings' deep-merge
            # still restores those keys from DEFAULTS, keeping the on-disk schema stable.
            "serial": {
                "default_baud": self._parse_int(self._baud_combo.currentText(), 115200),
            },
            "flash": {
                # "Auto" persists as None (use the firmware's baud); any concrete entry is an override.
                "flash_baud": self._gather_flash_baud(),
            },
            "vault": {
                "dir": self._vault_dir_edit.text().strip(),
            },
            "safety": {
                "confirm_dangerous": self._confirm_dangerous_check.isChecked(),
                "suppress_all_warnings": self._suppress_warnings_check.isChecked(),
            },
            "security": {
                "secure_container": self._secure_container_check.isChecked(),
            },
            # Preserve the one-time disclaimer ack: _gather rebuilds the whole dict,
            # so without carrying it forward a Save would reset it to False and
            # re-show the first-run disclaimer on next launch.
            "_disclaimer_ack": disk.get("_disclaimer_ack", False),
            # Same hazard for the interface section (Simple/Pro mode + the de-bloat loadout) and its
            # first-run ack — this tab owns no widgets for them, so rebuilding the dict without
            # carrying them forward makes save_settings' deep-merge reset mode to 'pro', DROP the
            # loadout, and reset _interface_mode_ack to False: a plain Save silently undoes the Simple
            # choice + loadout and re-arms both first-run choosers on the next launch.
            # Carry the interface section forward (Simple/Pro mode + loadout this tab owns no
            # widgets for), overlaying only the widget-backed touch_mode so a Save persists it
            # without wiping the mode/loadout.
            "interface": {
                **disk.get("interface", {}),
                "touch_mode": self._touch_mode_combo.currentData(),
            },
            "_interface_mode_ack": disk.get("_interface_mode_ack", False),
            # CRITICAL carry-forward: this tab owns only the `enabled` toggle for `updates`, but _gather
            # rebuilds the WHOLE settings dict. Without preserving the rest of the section, a plain Save
            # would wipe the suppression bookkeeping (suppressed / suppressed_at_behind / dismissed_version
            # / offline_error_suppressed / last_seen_latest / last_check_iso). Start from the on-disk block
            # and overlay only the widget-backed `enabled`.
            "updates": {
                **disk.get("updates", {}),
                "enabled": self._updates_enabled_check.isChecked(),
            },
            # The WiGLE token is the only widget-backed key in "uploads"; overlay it on the on-disk
            # block so any future upload bookkeeping isn't wiped by a plain Save (same carry-forward as updates).
            "uploads": {
                **disk.get("uploads", {}),
                "wigle_token": self._wigle_token_edit.text().strip(),
                "wdgwars_token": self._wdgwars_token_edit.text().strip(),
            },
        }

    # ── Actions ──────────────────────────────────────────────────────

    def _on_save(self) -> None:
        self._settings = self._gather()
        try:
            save_settings(self._settings)
            self._dirty = False  # persisted — the widgets now match disk, so a reload is safe again
            QMessageBox.information(self, "Settings", "Settings saved successfully.")
        except Exception as exc:  # noqa: BLE001 — surface any I/O error to the user
            log.exception("Failed to save settings")
            QMessageBox.critical(self, "Error", f"Failed to save settings:\n{exc}")

    def _on_reset(self) -> None:
        reply = QMessageBox.question(
            self,
            "Reset Settings",
            "Reset all fields to defaults? (You must Save to persist.)",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            # DEFAULTS now contains a top-level scalar (_disclaimer_ack), so guard
            # the dict() copy against non-dict values.
            self._settings = {
                k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()
            }
            self._load_into_ui(self._settings)
            self._dirty = True  # defaults differ from disk until Saved — don't let a tab switch revert them

    def _on_suppress_toggled(self, checked: bool) -> None:
        """One-time acknowledgement when ENABLING 'suppress all warnings'.

        Enabling it removes every per-command safety confirmation, so we make the
        user acknowledge once; cancelling re-unchecks the box.
        """
        if not checked:
            return
        reply = QMessageBox.warning(
            self,
            "Suppress all safety warnings",
            "This disables every per-command safety confirmation. Dangerous commands "
            "(deauth, jamming, beacon spam) will be sent with no prompt.\n\n"
            "You remain solely responsible for lawful, authorized use. Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            self._suppress_warnings_check.blockSignals(True)
            self._suppress_warnings_check.setChecked(False)
            self._suppress_warnings_check.blockSignals(False)

    def _on_secure_container_toggled(self, checked: bool) -> None:
        """When ENABLING the secure container, point out that it depends on the access gate.

        The container's encryption key lives only inside the unlocked vault, so without a configured
        gate the container can never unseal. Warn (but don't block) so the user can set the gate up.
        """
        if not checked:
            return
        try:
            from src.security import physical_key as pk
            configured = pk.is_configured()
        except Exception:  # noqa: BLE001
            configured = False
        if not configured:
            QMessageBox.information(
                self,
                "Secure container needs the access gate",
                "The secure container is encrypted with a key held inside the access-gate vault, so it "
                "stays sealed until you set up and unlock the gate.\n\n"
                "Enable this now if you like, then set up the access gate above so saves can be "
                "encrypted. Until the gate exists/unlocks, the app falls back to its normal (plaintext) "
                "save paths.",
            )

    def _on_gate_setup(self) -> None:
        """Open the access-gate setup dialog (set admin password / physical key / policy)."""
        try:
            from src.ui.qt.gate_setup_dialog import GateSetupDialog
            GateSetupDialog(self).exec_()
        except Exception as exc:  # noqa: BLE001 — never crash Settings on the optional dialog
            log.exception("Access-gate setup dialog failed")
            QMessageBox.critical(self, "Access Gate", f"Could not open access-gate setup:\n{exc}")
        self._refresh_gate_status()

    def _refresh_gate_status(self) -> None:
        try:
            from src.security import physical_key as pk
            cfg = pk.load_config()
            self._gate_status_lbl.setText(
                f"Status: configured={pk.is_configured()}  ·  policy={cfg.get('policy')}  ·  "
                f"password={'set' if cfg.get('password') else 'not set'}  ·  "
                f"key={'set' if cfg.get('key') else 'not set'}"
            )
        except Exception:  # noqa: BLE001
            self._gate_status_lbl.setText("Status: unavailable")

    def _refresh_update_status(self, updates: dict | None = None) -> None:
        """Repaint the update-status line from the given (or on-disk) ``updates`` settings block."""
        from src.version import __version__
        upd = updates if updates is not None else load_settings().get("updates", {})
        self._update_status_lbl.setText(format_update_status(__version__, upd))

    def _on_browse_vault(self) -> None:
        start = self._vault_dir_edit.text().strip() or ""
        path = QFileDialog.getExistingDirectory(self, "Select Firmware Vault Directory", start)
        if path:
            self._vault_dir_edit.setText(path)

    # ── Qt overrides ─────────────────────────────────────────────────

    def showEvent(self, event) -> None:  # noqa: N802 — Qt naming
        """Reload settings from disk when the tab becomes visible — but ONLY if there are no unsaved edits,
        so a tab round-trip (leave and return without Save) can't silently discard what the user typed. The
        reload exists to avoid showing values another component wrote; keep refreshing self._settings + the
        gate status regardless so external writes still surface, and only skip the widget repopulation."""
        super().showEvent(event)
        self._settings = load_settings()
        if not self._dirty:
            self._load_into_ui(self._settings)
        self._refresh_gate_status()
        # Always refresh the update status from disk — an on-launch/background check may have run since
        # the tab was last shown, and it's stored (last_check_iso) even when the widgets aren't reloaded.
        self._refresh_update_status(self._settings.get("updates", {}))

    # ── Accessors / helpers ──────────────────────────────────────────

    def get_settings(self) -> dict:
        """Return the most recently saved/loaded settings dict."""
        return self._settings

    @staticmethod
    def _set_combo_text(combo: QComboBox, text: str) -> None:
        idx = combo.findText(text)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        else:
            combo.setEditText(text)

    @staticmethod
    def _parse_int(text: str, fallback: int) -> int:
        try:
            return int(str(text).strip())
        except (TypeError, ValueError):
            return fallback

    def _gather_flash_baud(self) -> int | None:
        """"Auto" (or a blank/non-numeric entry) -> None so each firmware's own baud is used; a real
        number -> that explicit override applied to every flash and backup read."""
        text = self._flash_baud_combo.currentText().strip()
        if not text or text == _FLASH_BAUD_AUTO:
            return None
        try:
            return int(text)
        except (TypeError, ValueError):
            return None
