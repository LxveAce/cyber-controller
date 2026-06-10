"""Tests for ``src.core.profile_loader`` + ``FirmwareProfile.from_file``.

Covered (no network, no esptool, no device):
    * ``core_id_for`` maps the JSON profile ids whose flash-core key differs;
    * EVERY shipped ``src/config/profiles/*.json`` loads via
      ``FirmwareProfile.from_file`` and yields a non-empty ``core_id`` + ``chip``,
      with the ``core_id`` resolving into ``flash_core.PROFILES`` (or == 'custom').

``profile_loader`` / ``flash_engine`` / ``flash_core`` all import with only the
standard library, so no heavy optional dep is needed; the ``importorskip`` calls
are belt-and-suspenders.
"""

from __future__ import annotations

import pytest

from conftest import shipped_profile_paths

profile_loader = pytest.importorskip("src.core.profile_loader")
flash_core = pytest.importorskip("src.core.flash_core")
flash_engine = pytest.importorskip("src.core.flash_engine")

FirmwareProfile = flash_engine.FirmwareProfile


# ── core_id_for mapping ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "json_id, core_id",
    [
        ("ghost_esp", "ghostesp"),
        ("esp32_div", "esp32-div"),
        ("minigotchi", "minigotchi-v3"),
        ("flipper_momentum", "momentum"),
        ("marauder", "marauder"),
    ],
)
def test_core_id_for_mapping(json_id: str, core_id: str) -> None:
    assert profile_loader.core_id_for(json_id) == core_id


def test_core_id_for_empty_is_custom() -> None:
    assert profile_loader.core_id_for("") == "custom"


def test_core_id_for_unknown_passes_through() -> None:
    # An id not in the map and not empty maps to itself (then the engine treats a
    # non-PROFILES core_id as 'custom' at flash time).
    assert profile_loader.core_id_for("totally_new_fw") == "totally_new_fw"


# ── Every shipped profile loads and resolves ─────────────────────────

_PROFILE_PATHS = shipped_profile_paths()


def test_profiles_dir_is_populated() -> None:
    # Guards against a silently-empty glob making the parametrized tests vacuous.
    assert len(_PROFILE_PATHS) >= 14


# Backends whose flash is routed through ``flash_core.PROFILES`` (esptool images and
# the qFlipper .tgz profiles, which still resolve to momentum/unleashed core ids).
# The SD-image / ADB backends (kali-arm, raspyjack, pwnagotchi, rayhunter) are routed
# through sd_backend / adb_backend instead, so their core_id intentionally does NOT
# live in flash_core.PROFILES.
_FLASH_CORE_BACKENDS = {"esptool", "qflipper"}


@pytest.mark.parametrize("path", _PROFILE_PATHS, ids=lambda p: p.stem)
def test_shipped_profile_from_file(path) -> None:
    prof = FirmwareProfile.from_file(path)

    # Non-empty resolved core id — for EVERY shipped profile.
    assert prof.core_id, f"{path.name}: empty core_id"

    # Non-empty chip (explicit, first-board chip, or the 'auto' sentinel) — for EVERY profile.
    assert prof.chip, f"{path.name}: empty chip"

    # For the flash_core-routed backends, the core_id must resolve into the flash-core
    # registry (or be the local-bin 'custom' fallback). SD/ADB backends are exempt — they
    # flash via a different backend entirely.
    if prof.backend in _FLASH_CORE_BACKENDS:
        assert prof.core_id in flash_core.PROFILES or prof.core_id == "custom", (
            f"{path.name}: core_id {prof.core_id!r} not in PROFILES and not 'custom'"
        )
