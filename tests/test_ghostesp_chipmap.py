"""Regression: GhostESP ships one merged.bin per board, each compiled for a specific ESP-IDF target. CC's
chip_map only classified boards whose asset name contained an `s3`/`c5`/`c6` (or a few devkit) substring, so
**16** boards whose names carry no such hint fell to the classic-`esp32` default — the wrong `--chip`, which
esptool refuses on the real part before any write (or mis-targets on the full path).

The expected chip for every asset is GROUND TRUTH from GhostESP's own build config, NOT re-derived from the
resolver: `build.py get_build_targets()` (zip_name → idf_target) plus the standalone `configs/sdkconfig.<board>`
`CONFIG_IDF_TARGET` for the boards built outside that table (ACE/Banshee/XIAO/TDongle/NM-CYD/HeltecV3/
Poltergeist/FeberisPro). Sourced from GhostESP-Revival/GhostESP @ Development-deki; asset list = live v2.0
release. Network mocked.
"""
from __future__ import annotations

import pytest

flash_core = pytest.importorskip("src.core.flash_core")

# asset stub -> idf_target, from GhostESP build.py/sdkconfig (the chip each merged.bin was compiled for).
_EXPECTED = {
    "ACE_C5": "esp32c5", "ACE_S3": "esp32s3", "AwokMini": "esp32s2", "Banshee_C5": "esp32c5",
    "Banshee_S3": "esp32s3", "CardputerADV": "esp32s3", "Crowtech_LCD": "esp32s3", "CYD2432S028R": "esp32",
    "CYD2USB": "esp32", "CYD2USB2.4Inch": "esp32", "CYD2USB2.4Inch_C": "esp32", "CYDDualUSB": "esp32",
    "CYDMicroUSB": "esp32", "esp32-generic": "esp32", "ESP32-S3-Cardputer": "esp32s3",
    "esp32c3-generic": "esp32c3", "esp32c5-generic-v01": "esp32c5", "esp32c6-generic": "esp32c6",
    "esp32s2-generic": "esp32s2", "esp32s3-generic": "esp32s3", "esp32v5_awok": "esp32s2", "FeberisPro": "esp32",
    "Flipper_JCMK_GPS": "esp32s2", "ghostboard": "esp32c6", "HeltecV3": "esp32s3", "JC3248W535EN_LCD": "esp32s3",
    "JCMK_DevBoardPro": "esp32", "LilyGo-S3TWatch-2020": "esp32s3", "LilyGo-T-Deck": "esp32s3",
    "LilyGo-TDisplayS3-Touch": "esp32s3", "LilyGo-TDongleC5": "esp32c5", "LilyGo-TDongleS3": "esp32s3",
    "LilyGo-TEmbedC1101": "esp32s3", "Lolin_S3_Pro": "esp32s3", "MarauderPancake": "esp32c5",
    "MarauderV4_FlipperHub": "esp32", "MarauderV6_AwokDual": "esp32", "MarauderV8": "esp32c5", "NM-CYD-C5": "esp32c5",
    "Poltergeist": "esp32c5", "RabbitLabs_Minion": "esp32", "Sunton_LCD": "esp32s3", "Waveshare_LCD": "esp32s3",
    "XIAO_C5": "esp32c5", "XIAO_S3": "esp32s3", "XIAO_S3_Sense": "esp32s3",
}

# the 16 boards this fix rescued from the classic-esp32 default (name carries no s3/c5/c6 hint).
_RESCUED = {
    "esp32c3-generic": "esp32c3", "esp32s2-generic": "esp32s2", "AwokMini": "esp32s2", "esp32v5_awok": "esp32s2",
    "Flipper_JCMK_GPS": "esp32s2", "ghostboard": "esp32c6", "HeltecV3": "esp32s3", "JC3248W535EN_LCD": "esp32s3",
    "Crowtech_LCD": "esp32s3", "Sunton_LCD": "esp32s3", "Waveshare_LCD": "esp32s3", "LilyGo-T-Deck": "esp32s3",
    "LilyGo-TEmbedC1101": "esp32s3", "MarauderV8": "esp32c5", "MarauderPancake": "esp32c5", "Poltergeist": "esp32c5",
}


def _resolved(monkeypatch):
    assets = [{"name": f"{s}.zip", "browser_download_url": f"https://x/{s}"} for s in _EXPECTED]
    monkeypatch.setattr(flash_core, "_github_latest", lambda u: ("v2.0", assets))
    _tag, out = flash_core.get_profile("ghostesp").latest_release()
    return {a["name"]: a for a in out}


def test_every_asset_matches_ghostesp_build_target(monkeypatch):
    # each board must resolve to the exact idf_target GhostESP compiled its merged.bin for.
    by = _resolved(monkeypatch)
    assert len(by) == len(_EXPECTED), "every .zip asset must resolve"
    wrong = {s: by[f"{s}.zip"]["chip"] for s, c in _EXPECTED.items() if by[f"{s}.zip"]["chip"] != c}
    assert not wrong, f"chip mismatch vs GhostESP build config: {wrong}"


def test_rescued_boards_no_longer_default_to_classic_esp32(monkeypatch):
    # the 16 boards that used to mis-flash as classic esp32 now carry their true chip.
    by = _resolved(monkeypatch)
    for stub, chip in _RESCUED.items():
        got = by[f"{stub}.zip"]["chip"]
        assert got == chip != "esp32", f"{stub}: expected {chip}, got {got}"


def test_supported_chip_families():
    # esp32c3/c5/c6 bootloader @0x0; esp32s2 uses classic 0x1000 — all are recognized esptool chips in CC.
    for c in ("esp32c3", "esp32c5", "esp32c6"):
        assert c in flash_core._BOOTLOADER_0
    assert "esp32s2" not in flash_core._BOOTLOADER_0
