"""B2 — flash-default honesty: a blind Marauder "Auto" flash must not silently pick the generic
ILI9341 build (flash_core ``old_hardware``), which is the wrong display driver for most CYD panels
and leaves the screen blank after a "successful" flash.

The Flash tab now (a) names the generic guess in the "Auto" variant label for Marauder, (b) gates a
Marauder+Auto flash behind a confirm dialog (cancel aborts; confirm proceeds and arms a post-flash
"screen blank? re-pick" hint), and (c) leaves every explicit variant / non-Marauder profile flashing
straight through with no prompt.

Offscreen Qt; fixture + fake-worker patterns mirror test_flash_concurrency_guard.py.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QMessageBox  # noqa: E402

import src.ui.qt.flash_tab as flash_tab  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    import src.config.settings as S
    monkeypatch.setattr(S, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(S, "SETTINGS_PATH", tmp_path / "settings.json")
    return S


@pytest.fixture(autouse=True)
def _no_blocking_sd_probe(monkeypatch):
    import src.core.backends.sd_backend as sd
    monkeypatch.setattr(sd, "detect_sd_cards", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def _no_variant_network(monkeypatch):
    # The variant picker loads its list on a background QThread that hits the network. Keep this unit
    # test hermetic + deterministic by never starting that loader — the "Auto" item at index 0 (all we
    # rely on here) is added synchronously before the loader would run.
    monkeypatch.setattr(flash_tab._VariantLoader, "start", lambda self: None)


@pytest.fixture(autouse=True)
def _no_real_flash(monkeypatch):
    # Never spawn a real flash thread (network + hardware). Constructing the worker is fine (it only
    # stores refs); we just neuter start() so we can assert the flash was *reached* without running it.
    monkeypatch.setattr(flash_tab._FlashWorker, "start", lambda self: None)


@pytest.fixture
def flash_tab_widget(qapp, isolated_settings):
    from PyQt5.QtCore import QTimer

    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt.main_window import CyberControllerWindow

    bus = EventBus()
    win = CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))
    try:
        win._health.stop()
    except Exception:  # noqa: BLE001
        pass
    for t in win.findChildren(QTimer):
        t.stop()
    yield win._flash_tab
    try:
        win.close()
    except Exception:  # noqa: BLE001
        pass
    win.deleteLater()
    qapp.processEvents()


def _inject_port(ft, port: str = "COM_TEST") -> None:
    """Give _on_flash a selectable port (offscreen has no real ports)."""
    ft._port_combo.addItem(f"{port} — fake", port)
    ft._port_combo.setCurrentIndex(ft._port_combo.count() - 1)


def _marauder_name(ft) -> str:
    for name, path in ft._profiles.items():
        if ft._is_marauder(ft._fe.load_profile(path)):
            return name
    raise AssertionError("no Marauder profile found on disk")


def _first_non_marauder_name(ft) -> str:
    for name, path in ft._profiles.items():
        if not ft._is_marauder(ft._fe.load_profile(path)):
            return name
    raise AssertionError("no non-Marauder profile found on disk")


# ── the gate ─────────────────────────────────────────────────────────

def test_marauder_auto_cancel_aborts_flash(flash_tab_widget, monkeypatch):
    ft = flash_tab_widget
    _inject_port(ft)
    ft._profile_combo.setCurrentText(_marauder_name(ft))
    ft._variant_combo.setCurrentIndex(0)  # Auto (data "")
    assert ft._variant_combo.currentData() == ""

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Cancel))
    ft._worker = None
    ft._log_output.clear()

    ft._on_flash()

    assert ft._worker is None                                   # no flash spawned
    assert "cancelled" in ft._log_output.toPlainText().lower()  # the gate aborted the flash


def test_marauder_auto_yes_proceeds_and_arms_hint(flash_tab_widget, monkeypatch):
    ft = flash_tab_widget
    _inject_port(ft)
    ft._profile_combo.setCurrentText(_marauder_name(ft))
    ft._variant_combo.setCurrentIndex(0)

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Yes))
    ft._worker = None
    ft._log_output.clear()

    ft._on_flash()

    assert ft._worker is not None                                     # the flash was reached
    assert "generic ili9341" in ft._log_output.toPlainText().lower()  # honest log on confirm
    assert ft._pending_flash_hint                                     # post-flash hint armed

    # The armed hint surfaces on a successful completion, so a blank screen has a next step.
    ft._log_output.clear()
    ft._on_flash_done(True)
    done_log = ft._log_output.toPlainText().lower()
    assert "blank" in done_log and "re-flash" in done_log
    assert ft._pending_flash_hint is None                             # cleared after use


def test_marauder_explicit_variant_flashes_without_prompt(flash_tab_widget, monkeypatch):
    ft = flash_tab_widget
    _inject_port(ft)
    ft._profile_combo.setCurrentText(_marauder_name(ft))
    # An explicit board pick — not Auto — must never be second-guessed.
    ft._variant_combo.addItem("CYD 2.8\" ILI9341  (cyd_2432S028)", "cyd_2432S028")
    ft._variant_combo.setCurrentIndex(ft._variant_combo.count() - 1)

    called = {"warning": False}
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: called.__setitem__("warning", True) or QMessageBox.Cancel),
    )
    ft._worker = None
    ft._on_flash()

    assert called["warning"] is False   # no gate for an explicit variant
    assert ft._worker is not None       # flashed straight through
    assert ft._pending_flash_hint is None


def test_non_marauder_auto_flashes_without_prompt(flash_tab_widget, monkeypatch):
    ft = flash_tab_widget
    _inject_port(ft)
    ft._profile_combo.setCurrentText(_first_non_marauder_name(ft))
    ft._variant_combo.setCurrentIndex(0)  # Auto

    called = {"warning": False}
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: called.__setitem__("warning", True) or QMessageBox.Cancel),
    )
    ft._worker = None
    ft._on_flash()

    assert called["warning"] is False   # only Marauder+Auto is gated
    assert ft._worker is not None


# ── chip-awareness: a known S3/S2/C3 resolves Auto correctly, so no gate ─────

class _FakeDev:
    def __init__(self, port, board_type):
        self.port = port
        self.board_type = board_type
        self.name = "fake"


def test_marauder_auto_known_s3_chip_flashes_without_prompt(flash_tab_widget, monkeypatch):
    """A Marauder v6/v7 (ESP32-S3) with Auto resolves to the correct multiboardS3 build — the gate must
    NOT fire, or it would scare the user off a correct flash with a false 'generic ILI9341' claim."""
    from src.models.device import BoardType
    ft = flash_tab_widget
    monkeypatch.setattr(ft._dm, "scan_ports", lambda: [_FakeDev("COM_S3", BoardType.ESP32_S3)])
    ft._port_combo.addItem("COM_S3 — S3", "COM_S3")
    ft._port_combo.setCurrentIndex(ft._port_combo.count() - 1)
    ft._profile_combo.setCurrentText(_marauder_name(ft))
    ft._variant_combo.setCurrentIndex(0)  # Auto

    called = {"warning": False}
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: called.__setitem__("warning", True) or QMessageBox.Cancel),
    )
    ft._worker = None
    ft._on_flash()

    assert called["warning"] is False, "a known S3 chip flashes its correct build — no false-ILI9341 gate"
    assert ft._worker is not None


# ── the honest label (no per-chip guess baked into the picker) ───────────────

def test_auto_label_is_plain_for_marauder_and_others(flash_tab_widget):
    ft = flash_tab_widget
    for name in (_marauder_name(ft), _first_non_marauder_name(ft)):
        ft._profile_combo.setCurrentText(name)
        assert ft._variant_combo.itemText(0) == "Auto (default for chip)", (
            "Auto keeps its honest per-chip-default label; the CYD risk is surfaced by the flash-time "
            "gate, not a per-chip guess in the combo"
        )
        assert ft._variant_combo.itemData(0) == ""


# ── batch queue carries the variant + shares the same honest gate ────────────

def test_queue_carries_the_chosen_variant(flash_tab_widget):
    from PyQt5.QtCore import Qt
    ft = flash_tab_widget
    ft._profile_combo.setCurrentText(_marauder_name(ft))
    ft._variant_combo.addItem("CYD 2.8\" ILI9341  (cyd_2432S028)", "cyd_2432S028")
    ft._variant_combo.setCurrentIndex(ft._variant_combo.count() - 1)
    ft._port_combo.addItem("COM9 — fake", "COM9")
    ft._port_combo.setCurrentIndex(ft._port_combo.count() - 1)

    ft._add_to_queue()

    data = ft._queue_list.item(ft._queue_list.count() - 1).data(Qt.UserRole)
    assert data[2] == "cyd_2432S028", "the queued job must carry the picked variant (not drop it)"


def test_batch_flashes_the_carried_variant(flash_tab_widget, monkeypatch):
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QListWidgetItem
    ft = flash_tab_widget
    mar = _marauder_name(ft)
    it = QListWidgetItem(f"COM9 -> {mar} [cyd_2432S028]")
    it.setData(Qt.UserRole, ("COM9", mar, "cyd_2432S028"))
    ft._queue_list.addItem(it)

    captured = {}
    real_init = flash_tab._FlashWorker.__init__

    def _spy(self, engine, port, profile, *a, **k):
        captured["variant"] = profile.variant
        real_init(self, engine, port, profile, *a, **k)

    monkeypatch.setattr(flash_tab._FlashWorker, "__init__", _spy)
    monkeypatch.setattr(flash_tab._FlashWorker, "start", lambda self: self.finished.emit(True))

    ft._on_flash_queue()  # explicit variant -> no gate fires

    assert captured.get("variant") == "cyd_2432S028", "the batch must flash the queued variant"


def test_batch_marauder_auto_confirms_and_cancel_aborts(flash_tab_widget, monkeypatch):
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QListWidgetItem
    ft = flash_tab_widget
    mar = _marauder_name(ft)
    it = QListWidgetItem(f"COM_TEST -> {mar}")
    it.setData(Qt.UserRole, ("COM_TEST", mar, ""))  # Auto, unidentified port
    ft._queue_list.addItem(it)

    called = {"warning": False}
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: called.__setitem__("warning", True) or QMessageBox.Cancel),
    )
    ft._log_output.clear()

    ft._on_flash_queue()

    assert called["warning"] is True, "a Marauder+Auto batch confirms up front"
    assert ft._batch_jobs == [], "cancel aborts the batch — no jobs started"
    assert "cancelled" in ft._log_output.toPlainText().lower()


# ── UF2-family chip routing (Meshtastic nRF52840/RP2040/RP2350 -> uf2 backend) ──


def _mesh_profile():
    """Load the real Meshtastic profile (carries the chip_uf2_boards family in .raw)."""
    from src.core.resources import resource_path
    return flash_tab.FirmwareProfile.from_file(
        resource_path("src", "config", "profiles") / "meshtastic.json"
    )


def test_uf2_chip_for_variant_picks_uf2_family_chip():
    """A picked nRF52840/RP2040/RP2350 variant resolves to its chip so the flash routes to uf2."""
    prof = _mesh_profile()
    loaded = [
        {"name": "meshtastic-rak4631-2.7.26", "chip": "nrf52840"},
        {"name": "meshtastic-pico2-2.7.26", "chip": "rp2350"},
        {"name": "meshtastic-heltec-v3-2.7.26", "chip": "esp32s3"},  # esptool board
    ]
    assert flash_tab._uf2_chip_for_variant(prof, "meshtastic-rak4631-2.7.26", loaded) == "nrf52840"
    assert flash_tab._uf2_chip_for_variant(prof, "meshtastic-pico2-2.7.26", loaded) == "rp2350"
    # An esptool board's variant must NOT override chip (leaves auto-detect intact).
    assert flash_tab._uf2_chip_for_variant(prof, "meshtastic-heltec-v3-2.7.26", loaded) is None
    # Auto (empty variant) and unknown names never override.
    assert flash_tab._uf2_chip_for_variant(prof, "", loaded) is None
    assert flash_tab._uf2_chip_for_variant(prof, "meshtastic-nope-2.7.26", loaded) is None


def test_uf2_chip_for_variant_noop_without_family():
    """A profile with no chip_uf2_boards block never overrides the chip."""
    from src.core.flash_engine import FirmwareProfile
    plain = FirmwareProfile(backend="esptool", raw={"resolver_params": {}})
    loaded = [{"name": "x", "chip": "nrf52840"}]
    assert flash_tab._uf2_chip_for_variant(plain, "x", loaded) is None


def test_uf2_variant_chip_makes_engine_route_to_uf2():
    """End-to-end: setting the picked UF2 chip makes the engine dispatch to the uf2 backend."""
    from src.core.flash_engine import FlashEngine
    prof = _mesh_profile()
    loaded = [{"name": "meshtastic-rak4631-2.7.26", "chip": "nrf52840"}]
    chip = flash_tab._uf2_chip_for_variant(prof, "meshtastic-rak4631-2.7.26", loaded)
    prof.chip = chip  # exactly what _on_flash does
    assert FlashEngine()._uf2_family_backend(prof) == "uf2"
