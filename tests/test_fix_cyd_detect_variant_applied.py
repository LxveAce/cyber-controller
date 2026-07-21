"""Monitor 1.7.3/1.8.0 review fix: a CYD "Detect board" result must pre-select the detected variant
even when Marauder is ALREADY the current profile.

Bug (owner-reported "CYD detect is buggy"): in ``FlashTab._on_detect_done`` a successful detect
stores ``_pending_variant`` and, if Marauder is not current, switches to it (a reload then applies
the key via ``_on_variants_loaded``). But when Marauder is ALREADY current, no switch + no reload
fires, so ``_on_variants_loaded`` never runs; and ``_select_variant`` compares the detection KEY
("cyd_2432S028_2usb") against the combo's asset-NAME data ("..._cyd_2432S028_2usb.bin"), which never
matches -> the ``elif ... : pass`` silently dropped the variant. Net: the picker stayed on Auto and
Flash wrote the generic ILI9341 default over the panel detection just identified.

Offscreen Qt; fixture mirrors test_flash_default_variant_gate.py.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

import src.ui.qt.flash_tab as flash_tab  # noqa: E402
from src.core.cyd_detect import CydResult  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def flash_tab_widget(qapp, tmp_path, monkeypatch):
    import src.config.settings as S
    monkeypatch.setattr(S, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(S, "SETTINGS_PATH", tmp_path / "settings.json")
    import src.core.backends.sd_backend as sd
    monkeypatch.setattr(sd, "detect_sd_cards", lambda *a, **k: [])
    # Keep the variant picker hermetic: never start the background network loader, so the combo
    # holds only "Auto" at index 0 (the exact state the bug needs: the detected key is absent).
    monkeypatch.setattr(flash_tab._VariantLoader, "start", lambda self: None)

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


def _marauder_name(ft) -> str:
    for name, path in ft._profiles.items():
        if ft._is_marauder(ft._fe.load_profile(path)):
            return name
    raise AssertionError("no Marauder profile found on disk")


def _detect(variant: str, *, ambiguous: bool = False, confidence: str = "high",
            controller: str = "ST7789") -> CydResult:
    return CydResult(
        is_cyd=True, confidence=confidence, ambiguous=ambiguous, controller=controller,
        touch="resistive", variant=variant, label=variant, responded=True, raw="probe",
    )


def _name_with_id(ft, wanted: str) -> str:
    for name, path in ft._profiles.items():
        prof = ft._fe.load_profile(path)
        if str(getattr(prof, "id", "") or "").lower() == wanted:
            return name
    raise AssertionError(f"no profile with id={wanted!r} found on disk")


def _non_display_name(ft) -> str:
    """A profile that is neither Marauder nor display-capable — where Detect SHOULD steer to Marauder."""
    for name, path in ft._profiles.items():
        prof = ft._fe.load_profile(path)
        if ft._is_marauder(prof):
            continue
        boards = getattr(prof, "boards", None) or []
        if not any(isinstance(b, dict) and b.get("has_display") for b in boards):
            return name
    raise AssertionError("no non-display, non-Marauder profile found on disk")


def test_detect_applies_variant_when_marauder_already_selected(flash_tab_widget):
    ft = flash_tab_widget
    ft._profile_combo.setCurrentText(_marauder_name(ft))  # Marauder is ALREADY the current profile
    ft._variant_combo.setCurrentIndex(0)                  # Auto (data "")
    assert ft._variant_combo.currentData() == ""

    ft._on_detect_done(_detect("cyd_2432S028_2usb"))

    assert ft._variant_combo.currentData() == "cyd_2432S028_2usb", (
        "a CYD detect with Marauder already selected must pre-select the detected variant, not "
        "silently leave the picker on Auto (which flashes the generic ILI9341 over the panel)"
    )
    assert ft._pending_variant is None  # the pending key was consumed


def test_detect_3_5_inch_applied_when_marauder_already_selected(flash_tab_widget):
    # The 3.5" panel key (the owner's 3.5" board) must survive the already-Marauder path too.
    ft = flash_tab_widget
    ft._profile_combo.setCurrentText(_marauder_name(ft))
    ft._variant_combo.setCurrentIndex(0)
    ft._on_detect_done(_detect("cyd_3_5_inch"))
    assert ft._variant_combo.currentData() == "cyd_3_5_inch"
    assert ft._pending_variant is None


def test_non_cyd_result_leaves_picker_on_auto(flash_tab_widget):
    # Guard: a non-CYD / no-variant result must NOT touch the picker (early return, no pending set).
    ft = flash_tab_widget
    ft._profile_combo.setCurrentText(_marauder_name(ft))
    ft._variant_combo.setCurrentIndex(0)
    ft._on_detect_done(CydResult(is_cyd=False, responded=True, variant=""))
    assert ft._variant_combo.currentData() == ""
    assert ft._pending_variant is None


def test_detect_keeps_display_profile_instead_of_hijacking_to_marauder(flash_tab_widget):
    # P-13: a LxveOS user who runs Detect on their 3.5" CYD must STAY on LxveOS, not get silently
    # switched to Marauder (which dropped the panel choice). LxveOS declares an st7796 display board.
    ft = flash_tab_widget
    lxveos = _name_with_id(ft, "lxveos")
    ft._profile_combo.setCurrentText(lxveos)
    ft._variant_combo.setCurrentIndex(0)

    ft._on_detect_done(_detect("cyd_3_5_inch", controller="ST7796"))

    assert ft._profile_combo.currentText() == lxveos, (
        "Detect must keep a display-capable profile the user already selected, not jump to Marauder"
    )
    assert ft._pending_variant is None, (
        "no Marauder variant key should be left pending on a non-Marauder profile — it wouldn't map to "
        "that profile's own assets"
    )


def test_detect_steers_to_marauder_from_a_non_display_profile(flash_tab_widget):
    # The original helpful behavior is preserved: a user on a profile that can't flash a display gets
    # steered to Marauder with the detected variant pre-selected.
    ft = flash_tab_widget
    nondisp = _non_display_name(ft)
    ft._profile_combo.setCurrentText(nondisp)

    ft._on_detect_done(_detect("cyd_2432S028_2usb"))

    switched_to = ft._fe.load_profile(ft._profiles[ft._profile_combo.currentText()])
    assert ft._is_marauder(switched_to), (
        "a non-display profile should fall back to Marauder so the CYD panel can actually be flashed"
    )
    assert ft._pending_variant == "cyd_2432S028_2usb"  # remembered, applied when Marauder's list loads
