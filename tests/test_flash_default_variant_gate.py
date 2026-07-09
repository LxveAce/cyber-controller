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


# ── the honest label ─────────────────────────────────────────────────

def test_auto_label_names_ili9341_for_marauder(flash_tab_widget):
    ft = flash_tab_widget
    ft._profile_combo.setCurrentText(_marauder_name(ft))
    label = ft._variant_combo.itemText(0)
    assert "ili9341" in label.lower()
    assert "auto" in label.lower()
    # index 0 still carries the empty "use the per-chip default" sentinel
    assert ft._variant_combo.itemData(0) == ""


def test_auto_label_plain_for_non_marauder(flash_tab_widget):
    ft = flash_tab_widget
    ft._profile_combo.setCurrentText(_first_non_marauder_name(ft))
    assert ft._variant_combo.itemText(0) == "Auto (default for chip)"
