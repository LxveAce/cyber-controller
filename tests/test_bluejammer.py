"""Tests for the BlueJammer-V2 integration (EmenstaNougat/BlueJammer-V2).

Covers the flash profiles (esp32 + bw16), the pinned hashes, the critical esptool offset contract
(0x1000/0x8000/0x10000 with NO boot_app0), the rtl8720 bundle reuse, the illegal-tx labelling, the
id resolution, and the telemetry-only protocol (no sendable serial commands)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core import flash_core
from src.core import profile_loader
from src.core.flash_core import IMAGE_MULTI
from src.protocols import get_protocol, list_protocols


_PROFILES_DIR = Path(flash_core.__file__).resolve().parents[1] / "config" / "profiles"


# ── Profiles registered ──────────────────────────────────────────────

def test_both_bluejammer_profiles_registered():
    assert "bluejammer-esp32" in flash_core.PROFILES
    assert "bluejammer-bw16" in flash_core.PROFILES


def test_both_profiles_labelled_illegal_tx():
    # "label, never block": the firmware is retained + flashable but carries the strongest label.
    assert flash_core.get_profile("bluejammer-esp32").danger == "illegal-tx"
    assert flash_core.get_profile("bluejammer-bw16").danger == "illegal-tx"
    # and the label/description make it unmissable
    assert "LAB-ONLY" in flash_core.get_profile("bluejammer-esp32").label.upper()


# ── ESP32 engine: offsets + pinning ──────────────────────────────────

def test_esp32_release_is_app_at_0x10000_with_pinned_hash():
    p = flash_core.get_profile("bluejammer-esp32")
    assert p.image_model == IMAGE_MULTI
    assert p.app_offset("esp32") == "0x10000"
    tag, assets = p.latest_release()
    assert tag == "v0.2"
    assert len(assets) == 1
    a = assets[0]
    assert a["name"] == "BlueJammer-V2.ino.bin"
    assert a["offset"] == "0x10000"
    assert a["chip"] == "esp32"
    assert a["sha256"] == "6c77188ceb44a8a66126b87d51947403492d064f26e5596e21196626fd600a5b"
    assert "BlueJammer-V2/releases/download/v0.2/" in a["url"]


def test_esp32_support_files_have_no_boot_app0(monkeypatch):
    # The upstream flasher writes ONLY bootloader@0x1000 + partitions@0x8000 (no boot_app0 @0xE000).
    # Mock the network so we exercise the offset contract without downloading.
    fetched: list[str] = []

    def fake_download(url, cache, name, on_line):
        fetched.append(name)
        return f"/tmp/{name}"

    def fake_verify(path, expected, on_line):
        return None  # pinned-hash check is exercised separately; here we only test offsets

    monkeypatch.setattr(flash_core, "download_to", fake_download)
    monkeypatch.setattr(flash_core, "verify_sha256", fake_verify)

    support = flash_core.get_profile("bluejammer-esp32").support_files("esp32", "/tmp", lambda s: None)
    assert set(support.keys()) == {"0x1000", "0x8000"}
    assert "0xe000" not in support and "0xE000" not in support
    # both support files were fetched (and would have been hash-verified)
    assert "BlueJammer-V2.ino.bootloader.bin" in fetched
    assert "BlueJammer-V2.ino.partitions.bin" in fetched


# ── BW16 controller: AmebaD bundle reuse ─────────────────────────────

def test_bw16_bundle_reuses_rtl8720_boot_and_loader():
    p = flash_core.get_profile("bluejammer-bw16")
    _tag, assets = p.latest_release()
    by_name = {a["name"]: a for a in assets}
    assert set(by_name) == {
        "km0_boot_all.bin", "km4_boot_all.bin", "km0_km4_image2.bin",
        "imgtool_flashloader_amebad.bin",
    }
    # every asset is a pinned bundle member on the rtl8720 backend
    assert all(a.get("bundle") and a.get("sha256") and a["chip"] == "rtl8720" for a in assets)
    # km0/km4 boot images are byte-identical to the Vampire bundle (standard AmebaD boot)
    rtl = flash_core._RTL8720_BUNDLE_SHA256
    assert by_name["km0_boot_all.bin"]["sha256"] == rtl["km0_boot_all.bin"]
    assert by_name["km4_boot_all.bin"]["sha256"] == rtl["km4_boot_all.bin"]
    # the app image2 DIFFERS (it's the BlueJammer payload, not the Vampire one)
    assert by_name["km0_km4_image2.bin"]["sha256"] != rtl["km0_km4_image2.bin"]
    # the SRAM loader is reused from the rtl8720 base (BlueJammer doesn't ship it)
    assert by_name["imgtool_flashloader_amebad.bin"]["sha256"] == rtl["imgtool_flashloader_amebad.bin"]
    assert "vampel" in by_name["imgtool_flashloader_amebad.bin"]["url"]


# ── id resolution + JSON profiles ────────────────────────────────────

def test_json_ids_resolve_to_core_profiles():
    assert profile_loader.core_id_for("bluejammer_esp32") == "bluejammer-esp32"
    assert profile_loader.core_id_for("bluejammer_bw16") == "bluejammer-bw16"


@pytest.mark.parametrize("fname,backend", [
    ("bluejammer_esp32.json", "esptool"),
    ("bluejammer_bw16.json", "rtl8720"),
])
def test_json_profiles_present_and_labelled(fname, backend):
    data = json.loads((_PROFILES_DIR / fname).read_text(encoding="utf-8"))
    assert data["backend"] == backend
    assert data["protocol"] == "bluejammer"
    assert data["danger"] == "illegal-tx"
    assert profile_loader.core_id_for(data["id"]) in flash_core.PROFILES


def test_bw16_json_documents_web_control_surface():
    # "control as well": the control surface is documented (web UI), since there is no serial channel.
    data = json.loads((_PROFILES_DIR / "bluejammer_bw16.json").read_text(encoding="utf-8"))
    ctl = data["control"]
    assert ctl["type"] == "web"
    assert ctl["url"] == "http://192.168.1.1"
    assert "Bluetooth" in ctl["modes"] and "Idle" in ctl["modes"]


# ── Protocol: telemetry only, no command channel ─────────────────────

def test_bluejammer_protocol_registered_and_telemetry_only():
    assert "bluejammer" in list_protocols()
    proto = get_protocol("bluejammer")
    # NO sendable serial commands — control is the web UI / button, never CC over serial.
    assert proto.get_commands() == []
    # identifies its banner
    assert proto.identify("BlueJammer-V2 by @emensta starting...")
    assert not proto.identify("ESP32 Marauder v1.12.1")
    # parses telemetry into info/status events
    assert proto.parse_line("[MODE] BLE").event_type == "info"
    err = proto.parse_line("[ERROR] nRF24 not found")
    assert err.event_type == "status" and err.data["ok"] is False
    assert proto.parse_line("") is None
