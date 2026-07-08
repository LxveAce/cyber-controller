"""BUGHUNT-0708 #7: HaleHound must not offer its app-only OTA update image as a merged@0x0 flash.

HaleHound's asset_match matched every .bin and emitted offset 0x0 / merged:true for all of them —
including the OTA update image its own label_suffix_rules anticipate. That image is an app-only OTA
update (it belongs to the running firmware's OTA path), so cold-flashing it at 0x0 like the FULL merged
image is wrong. The live releases currently ship no .bin assets, so this is a latent config bug; these
lock the guard (asset_match exclude_substrings: OTA/ota) so a future OTA asset can't be mislabeled.

Network mocked; on-device correctness stays the Stage-5 hardware gate. Mirrors porkchop's narrower match.
"""
from __future__ import annotations

import pytest

flash_core = pytest.importorskip("src.core.flash_core")

# Names follow HaleHound's own label_suffix_rules convention: FULL = full merged image, OTA = update.
_ASSETS = [
    {"name": "HaleHound-CYD-v3.6.2-FULL.bin", "browser_download_url": "https://x/full"},
    {"name": "HaleHound-CYD-v3.6.2-OTA.bin", "browser_download_url": "https://x/ota"},
    {"name": "halehound-v355-release.png", "browser_download_url": "https://x/png"},  # non-bin -> excluded
]


def _resolve(monkeypatch, assets):
    prof = flash_core.get_profile("halehound")
    assert prof.image_model == flash_core.IMAGE_MERGED
    monkeypatch.setattr(flash_core, "_github_latest", lambda u: ("v3.6.2", [dict(a) for a in assets]))
    _tag, out = prof.latest_release()
    return out


def test_halehound_excludes_the_ota_update_image(monkeypatch):
    out = _resolve(monkeypatch, _ASSETS)
    names = [a["name"] for a in out]
    # the OTA app-only image must NOT be offered (it can't be cold-flashed at 0x0); the .png is dropped;
    # the FULL merged image survives at 0x0 / merged:true.
    assert not any("OTA" in n.upper() for n in names), f"OTA image must be excluded, got {names}"
    assert any("FULL" in n.upper() for n in names), f"FULL merged image must remain, got {names}"
    assert all(a.get("offset") == "0x0" and a.get("merged") is True for a in out), out


def test_halehound_lowercase_ota_also_excluded(monkeypatch):
    # the resolver's exclude is case-sensitive, so the profile lists both casings — a lowercase
    # `ota` filename must be dropped too, not just the uppercase one the label rule keys on.
    assets = [
        {"name": "halehound_full.bin", "browser_download_url": "https://x/full"},
        {"name": "halehound_ota.bin", "browser_download_url": "https://x/ota"},
    ]
    out = _resolve(monkeypatch, assets)
    assert [a["name"] for a in out] == ["halehound_full.bin"]


def test_halehound_plain_full_bin_still_resolves(monkeypatch):
    # A release with only the full merged image still yields exactly that one merged@0x0 asset.
    assets = [{"name": "HaleHound_FULL_merged.bin", "browser_download_url": "https://x/full"}]
    out = _resolve(monkeypatch, assets)
    assert len(out) == 1 and out[0].get("offset") == "0x0" and out[0].get("merged") is True
