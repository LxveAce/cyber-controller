"""GhostESP ships per-board .zip bundles (each with a flashable merged.bin), not bare
.bin assets. These tests cover the zip-aware variant discovery + the download_and_extract
helper, with _http_get / _github_latest mocked (no network, no hardware)."""
from __future__ import annotations

import io
import os
import zipfile

import pytest

from src.core import flash_core


def _make_zip(member: str = "merged.bin", data: bytes = b"MERGEDBIN" * 100) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("bootloader.bin", b"x" * 16)
        z.writestr("partitions.bin", b"y" * 16)
        z.writestr("firmware.bin", b"z" * 32)
        z.writestr(member, data)
    return buf.getvalue()


def test_download_and_extract_pulls_member(tmp_path, monkeypatch):
    payload = b"MERGEDBIN" * 100
    monkeypatch.setattr(flash_core, "_http_get", lambda url: _make_zip(data=payload))
    out = flash_core.download_and_extract(
        "https://example/esp32s3-generic.zip", str(tmp_path),
        "esp32s3-generic.zip", "merged.bin", lambda s: None)
    assert os.path.isfile(out)
    with open(out, "rb") as fh:
        assert fh.read() == payload
    assert out.endswith("merged.bin")


def test_download_and_extract_matches_member_by_basename(tmp_path, monkeypatch):
    # A nested member is still found by basename (and can't escape the cache dir).
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("build/out/merged.bin", b"NESTED")
    monkeypatch.setattr(flash_core, "_http_get", lambda url: buf.getvalue())
    out = flash_core.download_and_extract(
        "https://example/x.zip", str(tmp_path), "x.zip", "merged.bin", lambda s: None)
    assert os.path.realpath(out).startswith(os.path.realpath(str(tmp_path)))
    with open(out, "rb") as fh:
        assert fh.read() == b"NESTED"


def test_download_and_extract_missing_member_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(flash_core, "_http_get", lambda url: _make_zip(member="other.bin"))
    with pytest.raises(ValueError):
        flash_core.download_and_extract(
            "https://example/x.zip", str(tmp_path), "x.zip", "merged.bin", lambda s: None)


def test_ghostesp_zip_variant_discovery(monkeypatch):
    raw = [
        {"name": "esp32-generic.zip", "browser_download_url": "u1"},
        {"name": "esp32s3-generic.zip", "browser_download_url": "u2"},
        {"name": "LilyGo-TDisplayS3-Touch.zip", "browser_download_url": "u3"},
        {"name": "CYD2432S028R.zip", "browser_download_url": "u4"},
        {"name": "esp32c5-generic-v01.zip", "browser_download_url": "u5"},
        {"name": "esp32c6-generic.zip", "browser_download_url": "u6"},
        {"name": "release-notes.txt", "browser_download_url": "u7"},
    ]
    monkeypatch.setattr(flash_core, "_github_latest", lambda api: ("v1.9.10", raw))
    core = flash_core.get_profile("ghostesp")
    tag, assets = core.latest_release()
    assert tag == "v1.9.10"
    by = {a["name"]: a for a in assets}
    assert "release-notes.txt" not in by  # non-zip/bin skipped
    # every zip carries zip_member=merged.bin + offset 0x0 (merged image)
    for a in assets:
        assert a["zip_member"] == "merged.bin" and a["offset"] == "0x0" and a["merged"] is True
    # chip mapping (generic names + board-name heuristic)
    assert by["esp32-generic.zip"]["chip"] == "esp32"
    assert by["esp32s3-generic.zip"]["chip"] == "esp32s3"
    assert by["LilyGo-TDisplayS3-Touch.zip"]["chip"] == "esp32s3"
    assert by["CYD2432S028R.zip"]["chip"] == "esp32"
    assert by["esp32c5-generic-v01.zip"]["chip"] == "esp32c5"
    assert by["esp32c6-generic.zip"]["chip"] == "esp32c6"


def test_ghostesp_default_variant_prefers_generic(monkeypatch):
    raw = [
        {"name": "ESP32-S3-Cardputer.zip", "browser_download_url": "u1"},
        {"name": "esp32s3-generic.zip", "browser_download_url": "u2"},
        {"name": "ACE_S3.zip", "browser_download_url": "u3"},
    ]
    monkeypatch.setattr(flash_core, "_github_latest", lambda api: ("v1.9.10", raw))
    core = flash_core.get_profile("ghostesp")
    _tag, assets = core.latest_release()
    dv = core.default_variant(assets, "esp32s3")
    assert dv["name"] == "esp32s3-generic.zip"
