"""Regression tests for per-chip second-stage bootloader flash offsets (src/core/flash_core.py).

Writing the bootloader to the wrong address is a brick — the ROM can't find it on the next boot —
so the offsets MUST match esptool's per-target BOOTLOADER_FLASH_OFFSET. Pure, deterministic mapping
checks; no hardware, network, or serial device is touched.

Ported from universal-flasher's verified golden table (sweep #13, 2026-07-24): CC previously only
special-cased the C5, so ESP32-P4 / H4 fell through to the 0x1000 classic default (wrong — they use
0x2000, the same gotcha as the C5) and C61 fell to 0x1000 (wrong — it uses 0x0). A latent gap (no CC
profile maps p4/h4/c61 today, _detect_chip never emits them), but the table is now correct + tested.
"""
from __future__ import annotations

import pytest

from src.core import flash_core


@pytest.mark.parametrize("chip,offset", [
    ("esp32", "0x1000"),      # classic ESP32
    ("esp32s2", "0x1000"),
    ("esp32s3", "0x0"),
    ("esp32c2", "0x0"),
    ("esp32c3", "0x0"),
    ("esp32c6", "0x0"),
    ("esp32c61", "0x0"),      # <-- was falling through to 0x1000 before the fix
    ("esp32h2", "0x0"),
    ("esp32c5", "0x2000"),    # the well-known C5 gotcha: 0x2000, NOT 0x0
    ("esp32p4", "0x2000"),    # <-- was falling through to 0x1000 before the fix
    ("esp32h4", "0x2000"),    # <-- was falling through to 0x1000 before the fix
])
def test_bootloader_offset_matches_esptool(chip, offset):
    assert flash_core._bootloader_offset(chip) == offset


def test_c5_p4_h4_never_treated_as_zero_offset():
    # The 0x2000 chips must never be resolved to 0x0 (that silently bricks boot).
    for chip in ("esp32c5", "esp32p4", "esp32h4"):
        assert chip not in flash_core._BOOTLOADER_0
        assert flash_core._bootloader_offset(chip) == "0x2000"


def test_c61_resolves_to_zero_not_classic_default():
    # C61 is a RISC-V part (0x0); it must not fall through to the 0x1000 classic default.
    assert "esp32c61" in flash_core._BOOTLOADER_0
    assert flash_core._bootloader_offset("esp32c61") == "0x0"


def test_unknown_chip_falls_back_to_classic_default():
    # A chip in neither set is treated as classic ESP32 / S2 (0x1000) — the conservative default.
    assert flash_core._bootloader_offset("esp32-does-not-exist") == "0x1000"
