"""Tests for Dead Man's Switch partition-table resolution (src.core.suicide_setup.partitions_csv).

Regression guard for the guardian crash: guardian used to silently fall back to the fork table (which
has no `factory` partition) and blow up deep in the provisioner. It must resolve an exact guardian
table or fail with a clear, actionable message — never the fork layout.
"""

from __future__ import annotations

import pytest

suicide_setup = pytest.importorskip("src.core.suicide_setup")
from src.core.suicide_setup import SuicideConfig, partitions_csv  # noqa: E402


def _cfg(variant: str, flash: str) -> SuicideConfig:
    return SuicideConfig(variant=variant, flash_size=flash)


def test_guardian_8mb_resolves_to_bundled_table():
    path = partitions_csv(_cfg("guardian", "8MB"))
    assert path.name == "suicide_guardian_8MB.csv"
    assert path.is_file(), f"8MB guardian table missing: {path}"


def test_guardian_16mb_resolves():
    path = partitions_csv(_cfg("guardian", "16MB"))
    assert path.name == "suicide_guardian_16MB.csv"


@pytest.mark.parametrize("size", ["4MB", "8MB", "16MB"])
def test_fork_all_sizes_resolve(size):
    path = partitions_csv(_cfg("fork", size))
    assert path.name.startswith("suicide_") and "guardian" not in path.name


def test_guardian_4mb_refuses_clearly_and_never_falls_back_to_fork():
    with pytest.raises(ValueError) as ei:
        partitions_csv(_cfg("guardian", "4MB"))
    msg = str(ei.value)
    assert "Guardian" in msg and "Fork" in msg  # actionable, names the alternative
    assert "8MB" in msg and "16MB" in msg


def test_guardian_8mb_table_fills_flash_exactly():
    # Every partition offset+size must tile up to exactly 8 MB with no gap past the last region.
    path = partitions_csv(_cfg("guardian", "8MB"))
    end = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        off, size = int(parts[3], 16), int(parts[4], 16)
        end = max(end, off + size)
    assert end == 0x800000, f"8MB guardian table ends at {end:#x}, not 0x800000"
