"""Hydra32 / ESP32-Deauther — pinned-release, multi-file ESP32 profile (added 2026-06-29).

Offsets are verified against the upstream partitions.csv (branch devkit-v1):
bootloader@0x1000, partition-table@0x8000, factory app@0x10000, spiffs storage@0x190000.
SHA-256s are pinned from the actual 'Hydra32' release assets. Network is mocked here; real
on-device flash correctness remains the Stage-5 hardware gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

flash_core = pytest.importorskip("src.core.flash_core")
from src.core.flash_core import IMAGE_MULTI  # noqa: E402

_PROFILES_DIR = Path(__file__).resolve().parents[1] / "src" / "config" / "profiles"


def test_hydra32_registered_and_multifile():
    assert "hydra32" in flash_core.PROFILES
    p = flash_core.get_profile("hydra32")
    assert p.image_model == IMAGE_MULTI
    assert p.app_offset("esp32") == "0x10000"


def test_hydra32_app_asset_pinned():
    p = flash_core.get_profile("hydra32")
    tag, assets = p.latest_release()
    assert tag == "Hydra32"
    assert len(assets) == 1
    a = assets[0]
    assert a["name"] == "projecthydra-32.bin"
    assert a["chip"] == "esp32"
    assert a["offset"] == "0x10000"
    assert a["merged"] is False
    assert a["sha256"] == "f4052d06ff510b13c4b8f0682118482feb41537b7d228be35554640ffe6f0698"
    assert "ESP32-Deauther/releases/download/Hydra32/" in a["url"]


def test_hydra32_support_offsets(monkeypatch):
    fetched: list[str] = []
    monkeypatch.setattr(flash_core, "download_to", lambda url, cache, name, on_line: fetched.append(name) or f"/tmp/{name}")
    monkeypatch.setattr(flash_core, "verify_sha256", lambda path, expected, on_line: None)

    support = flash_core.get_profile("hydra32").support_files("esp32", "/tmp", lambda s: None)
    # bootloader + partition-table + spiffs storage; single-factory layout = NO boot_app0
    assert set(support.keys()) == {"0x1000", "0x8000", "0x190000"}
    assert "0xe000" not in support and "0xE000" not in support
    # Pinned support-file cache names are namespaced by profile id so two profiles that pin the SAME
    # basename (hydra32 and esp32_wifi_pentest both pin "bootloader.bin"/"partition-table.bin") can't
    # collide in the shared cache dir — the collision caused a false "integrity check failed" abort.
    assert {"hydra32_bootloader.bin", "hydra32_partition-table.bin",
            "hydra32_storage.bin"}.issubset(set(fetched))
    assert all(n.startswith("hydra32_") for n in fetched), f"names must be profile-namespaced: {fetched}"


def test_hydra32_lawful_framing():
    data = json.loads((_PROFILES_DIR / "hydra32.json").read_text(encoding="utf-8"))
    blob = (data["description"] + data["label"]).lower()
    assert "authorized" in blob  # deauth tool must carry the authorized-testing-only framing
    assert data["resolver"] == "pinned_release"
    assert data["resolver_params"]["verify_sha256"] is True
