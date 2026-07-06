"""_resolve_variant must not let a detection fragment match a superset-named asset. "cyd_2432S028" is a
substring of BOTH "..._cyd_2432S028.bin" (ILI9341) and "..._cyd_2432S028_2usb.bin" (ST7789) — the loose
substring matcher returned whichever GitHub happened to list first, so an unlucky order flashed the wrong
display driver (white/garbled screen) and silently defeated the Detect-board feature.
"""

from __future__ import annotations

import pytest

flash_engine = pytest.importorskip("src.core.flash_engine")


class _FakeCore:
    def __init__(self, names):
        self._names = names

    def variants_for_chip(self, assets, chip):
        return [{"name": n, "label": n} for n in self._names]


_BOTH_2USB_FIRST = [
    "esp32_marauder_v1_cyd_2432S028_2usb.bin",  # superset listed FIRST — the failure ordering
    "esp32_marauder_v1_cyd_2432S028.bin",
]


def _resolve(names, requested):
    fe = flash_engine.FlashEngine()
    return fe._resolve_variant(_FakeCore(names), None, "esp32", requested, lambda *_: None)


def test_plain_fragment_picks_plain_asset_despite_order():
    picked = _resolve(_BOTH_2USB_FIRST, "cyd_2432S028")
    assert picked["name"] == "esp32_marauder_v1_cyd_2432S028.bin"


def test_2usb_fragment_picks_2usb_asset():
    picked = _resolve(_BOTH_2USB_FIRST, "cyd_2432S028_2usb")
    assert picked["name"] == "esp32_marauder_v1_cyd_2432S028_2usb.bin"


def test_exact_name_still_wins():
    picked = _resolve(_BOTH_2USB_FIRST, "esp32_marauder_v1_cyd_2432S028.bin")
    assert picked["name"] == "esp32_marauder_v1_cyd_2432S028.bin"
