"""Characterization tests for src/core/device_detect.py pure detection logic.

Covers identify_usb (VID/PID -> label), match_firmware (banner -> name/version), and
detect_chip_from_text (banner -> chip), plus drift-locks on the lookup tables. All pure — no serial I/O.
"""

import pytest

dd = pytest.importorskip("src.core.device_detect")


# ── identify_usb ──────────────────────────────────────────────────────────
def test_identify_usb_none_vid_is_unknown():
    assert dd.identify_usb(None, 0x1234) == "unknown"


def test_identify_usb_exact_hit():
    assert dd.identify_usb(0x0483, 0x5740) == "Flipper Zero USB CDC"


def test_identify_usb_wildcard_pid_falls_back():
    # (0x1D6B, None) is a wildcard entry — any pid under that vid resolves to it.
    assert dd.identify_usb(0x1D6B, 0x9999) == "Linux USB gadget (Orbic RNDIS)"


def test_identify_usb_miss_with_pid_formats_hex():
    assert dd.identify_usb(0x9999, 0x1234) == "USB 9999:1234"


def test_identify_usb_miss_without_pid():
    assert dd.identify_usb(0x9999, None) == "USB 9999:????"


# ── match_firmware ────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,name,ver", [
    ("ESP32 Marauder v1.2.3", "marauder", "1.2.3"),
    ("GhostESP v2.0", "ghostesp", "2.0"),
    ("Bruce V1.0", "bruce", "1.0"),
])
def test_match_firmware_name_and_version(text, name, ver):
    assert dd.match_firmware(text) == (name, ver)


def test_match_firmware_bw16_banner_version_is_empty_string():
    # The RTL/BW16 signature's version group is [\d.]* — it matches with an EMPTY version, not None.
    name, ver = dd.match_firmware("RTL8720")
    assert name == "bw16"
    assert ver == ""


def test_match_firmware_no_match():
    assert dd.match_firmware("nothing recognizable here") == (None, None)


# ── detect_chip_from_text ─────────────────────────────────────────────────
@pytest.mark.parametrize("text,chip", [
    ("ESP32-S3 boot", "esp32s3"),   # specific variant beats the broad esp32 (checked first)
    ("ESP32-C5 rev", "esp32c5"),
    ("AmebaD ready", "rtl8720"),    # RTL family checked before the broad esp32
    ("ESP8266EX chip", "esp8266"),  # distinct chip; the bare \bESP32\b must not shadow it
    ("esp8266 lowercase", "esp8266"),  # esp8266 pattern IS case-insensitive (unlike bare esp32)
    ("ESP32 WROOM", "esp32"),
    ("STM32", "stm32"),
])
def test_detect_chip_from_text(text, chip):
    assert dd.detect_chip_from_text(text) == chip


def test_detect_chip_lowercase_esp32_is_case_sensitive_miss():
    # The bare esp32 pattern is \bESP32\b WITHOUT IGNORECASE — lowercase matches nothing.
    assert dd.detect_chip_from_text("esp32 only") is None


def test_detect_chip_no_match():
    assert dd.detect_chip_from_text("hello world") is None


# ── drift-locks on the lookup tables ──────────────────────────────────────
def test_usb_map_has_wildcard_and_known_entries():
    assert (0x1D6B, None) in dd.USB_DEVICE_MAP        # the one wildcard-pid entry
    assert (0x1A86, 0x7523) in dd.USB_DEVICE_MAP      # the ambiguous CH340 (ESP32/BW16)


def test_chip_patterns_check_rtl_before_broad_esp32():
    # Ordering is load-bearing: RTL8720/AmebaD must be tried before the broad \bESP32\b.
    keys = list(dd._CHIP_PATTERNS.keys())
    assert keys.index("rtl8720") < keys.index("esp32")
