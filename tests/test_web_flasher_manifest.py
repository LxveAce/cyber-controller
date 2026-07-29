"""The web-flasher manifest generator (scripts/gen_web_flasher_manifest.py) reuses flash_core's
resolution so the browser flasher writes exactly what the desktop does. These tests pin the
correctness-critical parts — the flash offsets, the segment layout per image model, and that a
marauder chip with no FlashFiles support mapping is skipped (not emitted with a broken full-flash) —
all with canned assets, no network."""

from __future__ import annotations

from scripts.gen_web_flasher_manifest import (
    build_manifest,
    manifest_for_assets,
    variant_entry,
)
from src.core import flash_core as fc


def _asset(name, url, chip, label="v"):
    return {"name": name, "url": url, "chip": chip, "label": label}


def test_merged_firmware_is_one_segment_at_app_offset():
    # ghost_esp ships a merged single .bin: one segment at the app offset (0x0), pointing at the
    # release asset URL verbatim.
    ghost = fc.get_profile("ghostesp")
    assert ghost.image_model == fc.IMAGE_MERGED
    entry = variant_entry(ghost, _asset("ESP32.bin", "https://x/ESP32.bin", "esp32"))
    assert entry["chip"] == "esp32"
    assert entry["segments"] == [{"offset": "0x0", "url": "https://x/ESP32.bin"}]


def test_zip_bundle_carries_the_member_to_extract():
    # GhostESP ships per-board .zip bundles, not bare .bins. flash_core's resolution tags the asset with
    # the member to unzip (merged.bin); the manifest must carry it through so the browser flasher extracts
    # the SAME file the desktop does instead of flashing the .zip raw.
    ghost = fc.get_profile("ghostesp")
    asset = _asset("ACE_S3.zip", "https://x/ACE_S3.zip", "esp32s3")
    asset["zip_member"] = "merged.bin"
    seg = variant_entry(ghost, asset)["segments"][0]
    assert seg["url"] == "https://x/ACE_S3.zip"
    assert seg["zip_member"] == "merged.bin"
    # a plain .bin asset must NOT gain a zip_member key
    plain = variant_entry(ghost, _asset("ESP32.bin", "https://x/ESP32.bin", "esp32"))["segments"][0]
    assert "zip_member" not in plain


def test_marauder_multi_file_segments_have_the_right_offsets_and_flashfiles_urls():
    # marauder ships the app .bin only; a full flash also needs bootloader/partitions/boot_app0 from
    # the repo FlashFiles tree. Segments in order: bootloader@chip-offset, partitions 0x8000,
    # boot_app0 0xe000, app 0x10000 — and the support URLs must be derived from the profile's OWN
    # support_files config via flash_core's URL helper (the same source the downloader uses).
    mar = fc.get_profile("marauder")
    assert mar.image_model == fc.IMAGE_MULTI
    sf = mar.cfg["support_files"]
    branch = sf["branches"][0]
    d = sf["support_dir_by_chip"]["esp32"]
    entry = variant_entry(mar, _asset("esp32_marauder_old_hw.bin", "https://x/app.bin", "esp32"))
    assert entry["segments"] == [
        {"offset": fc._bootloader_offset("esp32"),
         "url": fc._tree_url(sf, branch, sf["bootloader_path"].format(dir=d))},
        {"offset": sf["partitions_offset"],
         "url": fc._tree_url(sf, branch, sf["partitions_path"].format(dir=d))},
        {"offset": sf["boot_app0_offset"],
         "url": fc._tree_url(sf, branch, sf["boot_app0_path"].format(dir=d))},
        {"offset": "0x10000", "url": "https://x/app.bin"},   # app image, written last
    ]
    # concrete offset invariants (a wrong offset bricks a board): partitions/boot_app0/app
    assert [s["offset"] for s in entry["segments"]] == \
        [fc._bootloader_offset("esp32"), "0x8000", "0xe000", "0x10000"]


def test_marauder_chip_without_support_mapping_is_skipped_not_broken():
    # esp32c5 has no FlashFiles support-dir mapping, so it CANNOT be full-flashed from the browser.
    # It must be skipped (reported), never emitted with a partial/broken segment set.
    mar = fc.get_profile("marauder")
    assert "esp32c5" not in fc._SUPPORT_DIR
    assert variant_entry(mar, _asset("dual_mini_c5.bin", "https://x/c5.bin", "esp32c5")) is None

    built = manifest_for_assets(mar, "v1.13.0", [
        _asset("esp32_marauder_old_hardware.bin", "https://x/app.bin", "esp32"),
        _asset("dual_mini_c5.bin", "https://x/c5.bin", "esp32c5"),
    ])
    assert [v["chip"] for v in built["section"]["variants"]] == ["esp32"]
    assert len(built["skipped"]) == 1
    assert built["skipped"][0]["chip"] == "esp32c5"
    assert "support-file" in built["skipped"][0]["reason"]


def test_build_manifest_assembles_firmwares_and_skips(monkeypatch):
    # End-to-end wiring with the network stubbed: build_manifest resolves each flagship and folds
    # per-firmware skips into a single top-level list.
    def fake_latest(self):
        if self.id == "marauder":
            return "v1.13.0", [
                _asset("esp32_marauder_old_hardware.bin", "https://m/app.bin", "esp32"),
                _asset("dual_mini_c5.bin", "https://m/c5.bin", "esp32c5"),   # -> skipped
            ]
        if self.id == "ghostesp":
            return "v2.0", [_asset("ESP32.bin", "https://g/ESP32.bin", "esp32")]
        return "v1.0", [_asset("Bruce-ESP32.bin", "https://b/Bruce-ESP32.bin", "esp32")]

    # get_profile() returns GenericProfile instances (JSON-driven), which override latest_release —
    # patch there so the network fetch is stubbed for all three flagships.
    monkeypatch.setattr(fc.GenericProfile, "latest_release", fake_latest, raising=True)
    man = build_manifest(("marauder", "ghostesp", "bruce"))

    assert set(man["firmwares"]) == {"marauder", "ghostesp", "bruce"}
    assert man["firmwares"]["ghostesp"]["variants"][0]["segments"][0]["offset"] == "0x0"
    assert man["firmwares"]["bruce"]["image_model"] == fc.IMAGE_MERGED
    # the c5 marauder variant surfaced as a skip, not a broken variant
    assert [s["chip"] for s in man["skipped"]] == ["esp32c5"]
    assert len(man["firmwares"]["marauder"]["variants"]) == 1
