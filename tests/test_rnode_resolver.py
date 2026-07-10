"""RNode per_board_zip resolver mode — one per-board .zip whose members flash to DISTINCT offsets.

Unlike every other profile (single merged bin, or app + repo-tree boot chain), RNode packs the whole
boot chain (bootloader / partitions / boot_app0 / app / optional SPIFFS console_image) into ONE per-board
zip, each member at its own offset. These tests lock the resolver's variant emission: the correct chip,
app member/offset, a support_members list with the CHIP-DEPENDENT bootloader offset, and the absent-board
skip. The orchestration (flash_engine) extracts those members from the same cached zip — see the profile
note; the flash-argv shape itself is covered by the golden.
"""

from __future__ import annotations

import json

import pytest

flash_core = pytest.importorskip("src.core.flash_core")


def _rnode_params() -> dict:
    cfg = json.load(open("src/config/profiles/rnode.json", encoding="utf-8"))
    return cfg["resolver_params"]


def _raw(*boards: str) -> list:
    """A fake release asset list containing exactly the named board zips (+ release.json noise)."""
    out = [{"name": "release.json", "browser_download_url": "https://x/release.json"}]
    for b in boards:
        out.append({"name": f"rnode_firmware_{b}.zip",
                    "browser_download_url": f"https://x/rnode_firmware_{b}.zip"})
    return out


def test_emits_one_variant_per_present_board_and_skips_absent():
    p = _rnode_params()
    # profile configures 6 boards; supply only 4 in the "release" -> 2 skipped.
    raw = _raw("tbeam_sx1262", "t3s3", "heltec32v3", "xiao_esp32s3")  # lora32v21 + tdeck absent
    out = flash_core._expand_per_board_zip(p, raw)
    names = {v["name"] for v in out}
    assert len(out) == 4
    assert "rnode_firmware_lora32v21.zip" not in names  # absent -> skipped
    assert "rnode_firmware_tdeck.zip" not in names


def test_app_member_offset_and_url():
    p = _rnode_params()
    out = flash_core._expand_per_board_zip(p, _raw("t3s3"))
    v = out[0]
    assert v["chip"] == "esp32s3"
    assert v["zip_member"] == "rnode_firmware_t3s3.bin"
    assert v["offset"] == "0x10000"
    assert v["url"] == "https://x/rnode_firmware_t3s3.zip"
    assert v["label"] == "LilyGo T3S3 (SX1262)"


def test_bootloader_offset_is_chip_dependent():
    p = _rnode_params()
    out = flash_core._expand_per_board_zip(p, _raw("tbeam_sx1262", "t3s3"))
    by_chip = {v["chip"]: v for v in out}

    def boot_off(v):
        return next(m["offset"] for m in v["support_members"] if m["member"].endswith(".bootloader"))

    # classic ESP32 -> 0x1000, ESP32-S3 -> 0x0 (via _bootloader_offset)
    assert boot_off(by_chip["esp32"]) == "0x1000"
    assert boot_off(by_chip["esp32s3"]) == "0x0"


def test_support_members_full_boot_chain_with_optional_console():
    p = _rnode_params()
    v = flash_core._expand_per_board_zip(p, _raw("heltec32v3"))[0]
    sm = {m["member"]: m for m in v["support_members"]}
    assert sm["rnode_firmware_heltec32v3.partitions"]["offset"] == "0x8000"
    assert sm["rnode_firmware_heltec32v3.boot_app0"]["offset"] == "0xe000"
    assert sm["rnode_firmware_heltec32v3.bootloader"]["offset"] == "0x0"  # esp32s3
    # console_image is the shared SPIFFS member at 0x210000 and must be OPTIONAL (fw boots without it)
    assert sm["console_image.bin"]["offset"] == "0x210000"
    assert sm["console_image.bin"]["optional"] is True


def test_rnode_is_registered_and_builds():
    # The profile loads via the generic engine and lands in the live registry.
    assert "rnode" in flash_core.PROFILES
    prof = flash_core.PROFILES["rnode"]
    assert prof.danger == ""  # legit LoRa transport, not a jammer/deauth
    assert prof.image_model == flash_core.IMAGE_MULTI


# ── orchestration: build the full-flash support map from the per-board zip ──────────────────────

flash_engine = pytest.importorskip("src.core.flash_engine")


def _variant():
    p = _rnode_params()
    return flash_core._expand_per_board_zip(p, _raw("t3s3"))[0]  # esp32s3 -> bootloader @ 0x0


def test_support_map_extracts_every_member_at_its_offset(monkeypatch):
    calls = []

    def fake_extract(url, cache, asset_name, member, on_line):
        calls.append((asset_name, member))
        return f"/cache/{member}"

    monkeypatch.setattr(flash_engine.flash_core, "download_and_extract", fake_extract)
    support = flash_engine._support_from_zip_members(_variant(), "/cache", lambda *_: None)

    # every member is pulled from the ONE per-board zip and mapped to its flash offset
    assert all(a == "rnode_firmware_t3s3.zip" for a, _ in calls)
    assert support == {
        "0x0": "/cache/rnode_firmware_t3s3.bootloader",       # esp32s3 bootloader
        "0x8000": "/cache/rnode_firmware_t3s3.partitions",
        "0xe000": "/cache/rnode_firmware_t3s3.boot_app0",
        "0x210000": "/cache/console_image.bin",               # optional SPIFFS, present here
    }


def test_missing_optional_console_is_skipped_not_fatal(monkeypatch):
    def fake_extract(url, cache, asset_name, member, on_line):
        if member == "console_image.bin":
            raise RuntimeError("zip has no member console_image.bin")
        return f"/cache/{member}"

    monkeypatch.setattr(flash_engine.flash_core, "download_and_extract", fake_extract)
    support = flash_engine._support_from_zip_members(_variant(), "/cache", lambda *_: None)

    # the optional console image is simply absent; the required boot chain still maps
    assert "0x210000" not in support
    assert set(support) == {"0x0", "0x8000", "0xe000"}


def test_missing_required_member_raises(monkeypatch):
    def fake_extract(url, cache, asset_name, member, on_line):
        if member.endswith(".bootloader"):
            raise RuntimeError("zip has no member ...bootloader")  # a REQUIRED member is gone
        return f"/cache/{member}"

    monkeypatch.setattr(flash_engine.flash_core, "download_and_extract", fake_extract)
    with pytest.raises(RuntimeError):
        flash_engine._support_from_zip_members(_variant(), "/cache", lambda *_: None)
