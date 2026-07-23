"""Biscuit detection — grounded in the REAL HIL boot banners (2026-07-23,
command-center hil/biscuit-pro-serial-transcript-2026-07-23.txt).

The Biscuit Pro runs its own custom firmware (NOT Marauder) and exposes no serial command CLI on either
of its two ESPs — control is BLE-GATT-only. CC must recognize it by name and know never to serial-drive it.
"""
from __future__ import annotations

from src.core.device_detect import has_serial_cli, match_firmware

# Verbatim slices of the captured boot banners (verify-never-fake: what the chips actually printed).
WROOM_BANNER = (
    "  BISCUIT V1 - WROOM BLE Gateway\r\n  Version: v1.4.16\r\n"
    "[Storage] Device name: Biscuit Pro\r\nUART Bridge initialized\r\n"
    "C5 STATUS: C5 Scanner connected and ready\r\nBLE GATT Server initialized\r\n"
)
C5_BANNER = (
    "  *** NEW FIRMWARE BUILD ***\r\n  Board: Biscuit Pro\r\n  OTA: Biscuit_V1\r\n"
    "Version: v1.4.16\r\nESP-IDF version: v5.5.1-710-g8410210c9a\r\n"
    "  Ready for commands from WROOM\r\n"
)


def test_wroom_banner_detects_biscuit():
    fw, _ver = match_firmware(WROOM_BANNER)
    assert fw == "biscuit"


def test_c5_banner_detects_biscuit():
    fw, _ver = match_firmware(C5_BANNER)
    assert fw == "biscuit"


def test_biscuit_is_not_serial_controllable():
    assert has_serial_cli("biscuit") is False    # BLE-app-driven, no USB CLI
    assert has_serial_cli("marauder") is True     # a real serial CLI
    assert has_serial_cli("lxveos") is True
    assert has_serial_cli(None) is False          # unknown → don't blind-write


def test_biscuit_not_misdetected_as_marauder():
    # The old assumption was "biscuits use marauder"; the banner must NOT match marauder.
    fw, _ = match_firmware(WROOM_BANNER)
    assert fw != "marauder"


def test_biscuit_ultra_matched_by_name():
    # No Ultra HIL transcript yet — the Ultra shares the Pro's dual-ESP BLE-controlled architecture
    # (biscuitshop.us: "extended range, SD storage, 3x battery"), so it's matched by name until a real
    # Ultra capture confirms. Same family => same no-serial-CLI treatment.
    fw, _ = match_firmware("  BISCUIT V2 - WROOM BLE Gateway\r\n  Device name: Biscuit Ultra\r\n")
    assert fw == "biscuit"
    assert has_serial_cli("biscuit") is False
