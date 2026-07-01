"""Dead Man's Switch partition-CSV resolution (src/core/suicide_setup.py).

Regression guard: an unrecognized flash_size/variant must FAIL LOUD, not silently select the 4MB
layout — a wrong partition table bakes guardcfg at the wrong flash offset, so the firmware finds no
config, treats the board as unprovisioned, and boots with NO password gate while the owner believes
one is set. No hardware / submodule files needed (the resolver only builds the path)."""

from __future__ import annotations

import pytest

from src.core.suicide_setup import SuicideConfig, partitions_csv


def test_partitions_csv_canonicalizes_freeform_flash_size():
    assert partitions_csv(SuicideConfig(flash_size="16mb", variant="fork")).name == "suicide_16MB.csv"
    assert partitions_csv(SuicideConfig(flash_size="16 MB", variant="fork")).name == "suicide_16MB.csv"
    assert partitions_csv(SuicideConfig(flash_size="8mb", variant="fork")).name == "suicide_8MB.csv"


def test_partitions_csv_raises_instead_of_silent_4mb_fallback():
    # MUST NOT silently mint the 4MB layout for a bogus size.
    with pytest.raises(ValueError):
        partitions_csv(SuicideConfig(flash_size="16gb", variant="fork"))


def test_partitions_csv_valid_combos_unchanged():
    assert partitions_csv(SuicideConfig(flash_size="4MB", variant="fork")).name == "suicide_4MB.csv"
    assert partitions_csv(SuicideConfig(flash_size="16MB", variant="guardian")).name == "suicide_guardian_16MB.csv"
