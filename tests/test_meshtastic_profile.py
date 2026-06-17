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
    assert hv3["zip_member"] == "firmware-heltec-v3-2.7.15.abc.bin"
    assert hv3["offset"] == "0x0" and hv3["merged"] is True and hv3["url"] == "u_s3"

    e32 = core.variants_for_chip(assets, "esp32")
    assert any("tbeam" in a["name"] for a in e32)
    assert all(a["url"] == "u_e32" for a in e32)

    assert "heltec-v3" in core.default_variant(assets, "esp32s3")["name"]
    # debug-elfs + nrf zips yield no flashable variants
    assert not core.variants_for_chip(assets, "nrf52840")


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
