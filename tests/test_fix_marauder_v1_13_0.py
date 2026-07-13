"""Regression guard for the Marauder profile vs upstream justcallmekoko/ESP32Marauder v1.13.0.

The `marauder` profile resolves the *latest* upstream release live (resolver: github_release,
api_url -> /releases/latest), so the firmware version itself is never pinned and tracks upstream
automatically. What does NOT auto-update is the static asset->chip / asset->label mapping in
`resolver_params` — when upstream ships a new per-board .bin, an unmapped fragment silently falls
through to the `default` chip (esp32) and a short mislabel.

v1.13.0 (2026-07-07) added `dual_mini_c5` (a dual-band ESP32-C5 Mini). Before the fix it mapped to
chip `esp32` (wrong — a C5 image flashed as esp32 mismatches) and mislabeled as "Marauder Mini" via
the 4-char `mini` key. This test runs every real v1.13.0 asset name through the SHIPPED profile's
mapping functions and asserts the correct chip + a genuine (non-raw) label for all 24.

Pure logic against the real profile JSON + the real resolver helpers — no network, no hardware.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.core.flash_core import _chip_from_spec, _emit_label

_PROFILE = json.loads(
    (Path(__file__).resolve().parents[1] / "src" / "config" / "profiles" / "marauder.json").read_text(
        encoding="utf-8"
    )
)
_RP = _PROFILE["resolver_params"]
_CHIP_MAP = _RP["chip_map"]
_EMIT = _RP["emit"]

_PREFIX = "esp32_marauder_v1_13_0_20260707_"

# The 24 real assets published on justcallmekoko/ESP32Marauder v1.13.0, fragment -> expected chip.
_EXPECTED_CHIP = {
    "cyd_2432S024_guition": "esp32",
    "cyd_2432S028": "esp32",
    "cyd_2432S028_2usb": "esp32",
    "cyd_3_5_inch": "esp32",
    "dual_mini_c5": "esp32c5",   # the v1.13.0 addition — was defaulting to esp32
    "esp32c5devkitc1": "esp32c5",
    "esp32_lddb": "esp32",
    "flipper": "esp32s2",
    "kit": "esp32",
    "m5cardputer": "esp32s3",
    "m5cardputer_adv": "esp32s3",
    "m5nanoc6": "esp32c6",
    "m5stickc_plus": "esp32",
    "m5stickc_plus2": "esp32",
    "marauder_dev_board_pro": "esp32",
    "marauder_v7": "esp32",
    "mini": "esp32",
    "mini_v3": "esp32c5",
    "multiboardS3": "esp32s3",
    "old_hardware": "esp32",
    "rev_feather": "esp32s2",
    "v6": "esp32",
    "v6_1": "esp32",
    "v8": "esp32",
}


def _asset(frag: str) -> str:
    return f"{_PREFIX}{frag}.bin"


def test_dual_mini_c5_resolves_to_esp32c5_not_default_esp32():
    name = _asset("dual_mini_c5")
    assert _chip_from_spec(_CHIP_MAP, name, None) == "esp32c5"
    # And it gets its own label, not the short "mini" fallback.
    label = _emit_label(_EMIT, name, None)
    assert "C5" in label and label != name
    assert label != "Marauder Mini"


def test_all_v1_13_0_assets_map_to_correct_chip():
    for frag, expected in _EXPECTED_CHIP.items():
        name = _asset(frag)
        got = _chip_from_spec(_CHIP_MAP, name, None)
        assert got == expected, f"{frag}: expected chip {expected}, got {got}"


def test_all_v1_13_0_assets_get_a_human_label():
    # Every shipped asset must match a label_map key (longest-substring); a returned label equal to the
    # raw asset filename means it fell through unlabeled.
    for frag in _EXPECTED_CHIP:
        name = _asset(frag)
        label = _emit_label(_EMIT, name, None)
        assert label != name, f"{frag}: no label_map entry matched (unlabeled variant)"
