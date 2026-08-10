"""Regression lock for the POSEIDON flash profile (GeneralDussDuss/poseidon).

Pins the invariants beat-1's adversarial review established, so a later edit can't regress them:
  * the merged factory image is flashed at 0x0 on esp32s3 (a wrong offset bricks the board);
  * the resolver selects poseidon-factory.bin ONLY, never the poseidon-launcher.bin app-slot variant
    (which would want 0x10000) or the separate TRIDENT ESP32-C5 bins;
  * the v0.6.8 factory image is SHA-256-pinned to the real hash;
  * the firmware is labelled illegal-tx (it ships a 2.4GHz CW/broadband + sub-GHz jammer) — this was
    corrected from "" during the beat-1 adversarial pass, so lock it against a silent revert.

Network-free: the resolver test monkeypatches _github_latest with a synthetic release payload.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.core import flash_core

_PROFILES_DIR = Path(flash_core.__file__).resolve().parents[1] / "config" / "profiles"
_POSEIDON = json.loads((_PROFILES_DIR / "poseidon.json").read_text(encoding="utf-8"))

# sha256 of the real v0.6.8 poseidon-factory.bin (downloaded + verified in beat 1).
_V068_FACTORY_SHA = "99f9fa82d83a0e783263d058b5f6dc430477c57e5e77c65b4eb7b04fd12fc41f"


def test_poseidon_registered_and_ids_match():
    assert "poseidon" in flash_core.PROFILES
    # core_id must equal id (the registry key) or the loader self-maps and drifts.
    assert _POSEIDON["id"] == _POSEIDON["core_id"] == "poseidon"


def test_poseidon_is_merged_single_bin_at_0x0_esp32s3():
    p = flash_core.get_profile("poseidon")
    assert p.image_model == flash_core.IMAGE_MERGED
    # a merged factory image carries its own bootloader and is written at 0x0, not 0x10000.
    assert p.app_offset("esp32s3") == "0x0"
    assert _POSEIDON["boards"][0]["chip"] == "esp32s3"


def test_poseidon_labelled_illegal_tx_and_flash_only():
    # "label, never block": Poseidon ships a real CW/broadband + sub-GHz jammer, so it gets the
    # strongest label (like bluejammer). Corrected from "" in beat 1 — locked here.
    assert flash_core.get_profile("poseidon").danger == "illegal-tx"
    # flash-only: keyboard-driven standalone, no fabricated serial command protocol.
    assert _POSEIDON["protocol"] is None


def test_poseidon_factory_sha_pinned_for_v068():
    assert _POSEIDON.get("firmware_sha256", {}).get("v0.6.8") == _V068_FACTORY_SHA


def test_poseidon_resolver_selects_factory_only(monkeypatch):
    # A synthetic release carrying the factory bin, the launcher app-slot variant, AND an unrelated
    # TRIDENT bin: the resolver must emit ONLY the factory image, at 0x0, on esp32s3, merged.
    fake_assets = [
        {"name": "poseidon-launcher.bin",
         "browser_download_url": "https://github.com/GeneralDussDuss/poseidon/"
                                 "releases/download/v0.6.8/poseidon-launcher.bin"},
        {"name": "poseidon-factory.bin",
         "browser_download_url": "https://github.com/GeneralDussDuss/poseidon/"
                                 "releases/download/v0.6.8/poseidon-factory.bin"},
        {"name": "trident-c5-app.bin",
         "browser_download_url": "https://github.com/GeneralDussDuss/poseidon/"
                                 "releases/download/v0.6.8/trident-c5-app.bin"},
    ]
    monkeypatch.setattr(flash_core, "_github_latest", lambda api_url: ("v0.6.8", fake_assets))
    tag, assets = flash_core._resolve_github(_POSEIDON)
    assert tag == "v0.6.8"
    names = [a["name"] for a in assets]
    assert names == ["poseidon-factory.bin"], f"launcher/trident must be excluded, got {names}"
    a = assets[0]
    assert a["chip"] == "esp32s3"
    assert a["offset"] == "0x0"
    assert a["merged"] is True
