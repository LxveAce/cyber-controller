"""Dead Man's Switch chip canonicalization (src/core/suicide_setup.py).

Regression guard: the target chip must be canonicalized + allow-listed BEFORE it reaches the
provisioner. The provisioner derives the 2nd-stage bootloader offset with an EXACT membership test
(S3/C3/C6/H2 -> 0x0, else 0x1000), so a non-exact spelling of an S3/RISC-V part (e.g. Espressif's own
'ESP32-S3' branding) used to flow straight through and silently default to the classic 0x1000 offset
— baking a bundle whose bootloader lands at 0x1000 while the ROM loader reads 0x0, soft-bricking the
board while the tool reports success. build() must now fail LOUD (ValueError) on an unknown chip and
map hyphen/case/shorthand variants to the canonical esptool name.

Mirrors test_suicide_partitions_csv.py (fail-loud on a bad flash_size)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.suicide_setup import SuicideConfig, _canon_chip, build


def test_canon_chip_maps_branding_and_shorthand_variants():
    # Espressif brands the part 'ESP32-S3' (hyphen) — a realistic operator input.
    assert _canon_chip("ESP32-S3") == "esp32s3"
    assert _canon_chip("esp32-s3") == "esp32s3"
    assert _canon_chip("ESP32S3") == "esp32s3"
    assert _canon_chip("  esp32_s3 ") == "esp32s3"
    assert _canon_chip("s3") == "esp32s3"          # bare-suffix shorthand
    assert _canon_chip("C3") == "esp32c3"
    assert _canon_chip("ESP32-C6") == "esp32c6"
    assert _canon_chip("H2") == "esp32h2"
    # classic part is untouched
    assert _canon_chip("ESP32") == "esp32"
    assert _canon_chip("esp32s2") == "esp32s2"


def test_canon_chip_raises_on_unknown():
    with pytest.raises(ValueError):
        _canon_chip("esp32s9")
    with pytest.raises(ValueError):
        _canon_chip("banana")


def test_build_fails_loud_on_unknown_chip(tmp_path: Path):
    # build() must reject a bogus chip up front (before loading the provisioner) rather than forward
    # it verbatim and let the provisioner default to the 0x1000 offset.
    cfg = SuicideConfig(chip="totally-not-a-chip")
    with pytest.raises(ValueError):
        build(cfg, "correct horse", tmp_path / "bundle")


def test_canon_chip_yields_correct_bootloader_offset_downstream():
    """Tie the canonicalizer to the REAL provisioner: the canonical name must give the S3 bootloader
    offset 0x0, whereas the raw free-form 'ESP32-S3' string gives the classic 0x1000 (the exact bug
    the canonicalizer prevents)."""
    from src.core.suicide_setup import _load_provision

    try:
        prov = _load_provision()
    except FileNotFoundError:
        pytest.skip("deadmans-switch submodule not initialised")

    # The bug the fix prevents: the raw operator input defaults to the classic-ESP32 offset.
    assert prov.bootloader_offset("ESP32-S3") == 0x1000
    # The fix: canonicalizing first yields the correct S3/RISC-V offset (0x0).
    assert prov.bootloader_offset(_canon_chip("ESP32-S3")) == 0x0
    for raw, expected in (("esp32-c3", 0x0), ("C6", 0x0), ("h2", 0x0), ("ESP32", 0x1000),
                          ("esp32-s2", 0x1000)):
        assert prov.bootloader_offset(_canon_chip(raw)) == expected
