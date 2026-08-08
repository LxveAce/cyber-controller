"""HIL-grounded GhostESP detection regression.

Real-HW finding (HIL capture 2026-08-08, machine `extra`, COM35 freshly flashed with GhostESP via
the app's FlashEngine): the version regex `GhostESP\\s+v?…` MISSED the running firmware — GhostESP's
`help` prints "Ghost ESP Command Categories:" (a SPACE, no version) with a `ghost>` prompt, so
`match_firmware` returned None (flash worked, auto-detect didn't). Fixed by adding a GhostESP
fingerprint. This test pins the real captured output so detection can't silently regress.
"""
from __future__ import annotations

from src.core.device_detect import match_firmware

# Verbatim from the COM35 capture (trimmed): GhostESP's `help` reply.
REAL_GHOSTESP_HELP = """ghost> help
Ghost ESP Command Categories:
help wifi      - Wi-Fi commands
help ble       - Bluetooth/BLE commands
help comm      - ESP32 communication commands
help sd        - SD card commands
help gps       - GPS commands
help wigle     - WiGLE commands
help portal    - Evil Portal commands
help printer   - Printer commands
help cast      - YouTube cast commands
help capture   - Wi-Fi packet capture commands
help beacon    - Beacon spam commands
help attack    - Attack/flood commands
ghost>"""


def test_real_ghostesp_help_is_detected():
    fw, _ver = match_firmware(REAL_GHOSTESP_HELP)
    assert fw == "ghostesp"


def test_ghostesp_fingerprint_does_not_false_positive_on_marauder():
    # Marauder's own help must still resolve to marauder, never ghostesp.
    fw, _ = match_firmware("sniffbeacon sniffdeauth evilportal wardrive")
    assert fw == "marauder"


def test_unrelated_serial_noise_is_not_ghostesp():
    for noise in ("boot ok", "ets Jul 29 2019", "Registered Commands", "scanall  list -a"):
        fw, _ = match_firmware(noise)
        assert fw != "ghostesp"
