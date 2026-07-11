"""Meshtastic moved to per-CHIP zip bundles (firmware-<chip>-<ver>.zip), each containing every
board's factory bin. These tests cover the chip-zip + curated-board discovery and the cache-reuse
in download_and_extract (so the 128 MB bundle isn't re-fetched per board). _github_latest mocked."""
from __future__ import annotations

import io
import zipfile

from src.core import flash_core


def test_meshtastic_chip_zip_discovery(monkeypatch):
    raw = [
        {"name": "firmware-esp32s3-2.7.15.abc.zip", "browser_download_url": "u_s3"},
        {"name": "firmware-esp32-2.7.15.abc.zip", "browser_download_url": "u_e32"},
        {"name": "firmware-nrf52840-2.7.15.abc.zip", "browser_download_url": "u_nrf"},  # not esptool
        {"name": "debug-elfs-esp32s3-2.7.15.abc.zip", "browser_download_url": "u_dbg"},  # skipped
    ]
    monkeypatch.setattr(flash_core, "_github_latest", lambda api: ("v2.7.15.abc", raw))
    core = flash_core.get_profile("meshtastic")
    _tag, assets = core.latest_release()

    s3 = core.variants_for_chip(assets, "esp32s3")
    hv3 = next(a for a in s3 if "heltec-v3" in a["name"])
    assert hv3["zip_name"] == "firmware-esp32s3-2.7.15.abc.zip"
    # MERGED image at 0x0 must be the .factory.bin; the app-only .bin is the 0x10000 update image
    # and bricks if written at 0x0 (QA 2026-07-10). oracle + meshtastic.json member_template lockstep.
    assert hv3["zip_member"] == "firmware-heltec-v3-2.7.15.abc.factory.bin"
    assert hv3["offset"] == "0x0" and hv3["merged"] is True and hv3["url"] == "u_s3"

    e32 = core.variants_for_chip(assets, "esp32")
    assert any("tbeam" in a["name"] for a in e32)
    assert all(a["url"] == "u_e32" for a in e32)

    assert "heltec-v3" in core.default_variant(assets, "esp32s3")["name"]
    # debug-elfs is skipped; the nrf52840 zip now yields UF2 (drag-drop) variants, tagged
    # flash_method="uf2" so the engine routes them to the uf2 backend instead of esptool.
    nrf = core.variants_for_chip(assets, "nrf52840")
    rak = next(a for a in nrf if "rak4631" in a["name"])
    assert rak["zip_name"] == "firmware-nrf52840-2.7.15.abc.zip"
    assert rak["zip_member"] == "firmware-rak4631-2.7.15.abc.uf2"  # the .uf2, not the .hex/-ota.zip
    assert rak["flash_method"] == "uf2" and rak["url"] == "u_nrf"
    assert "offset" not in rak  # UF2 self-addresses — no offset to mis-fabricate


def test_meshtastic_esp32_dead_slugs_pruned(monkeypatch):
    """The 4 dead esp32 slugs (verified absent from the 2.7.26 manifest) must not resurface as
    broken variants; esp32c3 must resolve the current slugs, not the stale ones."""
    raw = [
        {"name": "firmware-esp32-2.7.26.abc.zip", "browser_download_url": "u_e32"},
        {"name": "firmware-esp32c3-2.7.26.abc.zip", "browser_download_url": "u_c3"},
    ]
    monkeypatch.setattr(flash_core, "_github_latest", lambda api: ("v2.7.26.abc", raw))
    core = flash_core.get_profile("meshtastic")
    _tag, assets = core.latest_release()

    e32_names = {a["name"] for a in core.variants_for_chip(assets, "esp32")}
    for dead in ("tbeam0_7", "heltec-v1", "heltec-v2_0", "heltec-v2_1"):
        assert not any(dead in n for n in e32_names), f"dead slug {dead} still emitted"

    c3_names = {a["name"] for a in core.variants_for_chip(assets, "esp32c3")}
    assert any("heltec-hru-3601" in n for n in c3_names)
    assert not any("esp32-c3-devkitm-1" in n for n in c3_names)  # stale slug gone


def test_uf2_family_backend_routes_by_chip():
    """The flash dispatch must send a UF2-family chip to the uf2 backend, and leave esp32* alone."""
    import json

    from src.core.flash_engine import FirmwareProfile, FlashEngine
    from src.core.resources import resource_path

    mesh = json.loads(
        (resource_path("src", "config", "profiles") / "meshtastic.json").read_text("utf-8")
    )
    eng = FlashEngine()
    for chip in ("nrf52840", "rp2040", "rp2350"):
        prof = FirmwareProfile(backend="esptool", chip=chip, raw=mesh)
        assert eng._uf2_family_backend(prof) == "uf2", chip
    # ESP32 family + auto-detect stay on the default (esptool) backend.
    for chip in ("esp32s3", "auto"):
        p = FirmwareProfile(backend="esptool", chip=chip, raw=mesh)
        assert eng._uf2_family_backend(p) is None
    # A profile with no chip_uf2_boards block never overrides.
    assert eng._uf2_family_backend(FirmwareProfile(backend="esptool", chip="nrf52840")) is None


def test_download_and_extract_reuses_cache(tmp_path, monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("firmware-heltec-v3-2.7.15.abc.bin", b"FACTORY")
    calls = {"n": 0}

    def fake_http(url):
        calls["n"] += 1
        return buf.getvalue()

    monkeypatch.setattr(flash_core, "_http_get", fake_http)
    a = flash_core.download_and_extract("u", str(tmp_path), "firmware-esp32s3-2.7.15.abc.zip",
                                        "firmware-heltec-v3-2.7.15.abc.bin", lambda s: None)
    b = flash_core.download_and_extract("u", str(tmp_path), "firmware-esp32s3-2.7.15.abc.zip",
                                        "firmware-heltec-v3-2.7.15.abc.bin", lambda s: None)
    assert calls["n"] == 1  # the second call reused the cached chip zip (no re-download)
    with open(a, "rb") as fa, open(b, "rb") as fb:
        assert fa.read() == fb.read() == b"FACTORY"
