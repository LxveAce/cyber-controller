"""batch.BatchFlasher / FlashEngine flash-path parity (dedupe the drifting hand-copies).

The status pass found BatchFlasher._flash_one was a hand-maintained parallel copy of
FlashEngine._flash_esptool, self-labeled "parity with FlashEngine" and drifting. Both flash paths
now share flash_core.download_variant_image (the zip-vs-raw choice), and batch resolves the write
offset the SAME way FlashEngine does — variant.get("offset") or <core>.app_offset(chip) — instead of
silently ignoring a variant's explicit offset. These tests lock that shared behavior.
"""
from __future__ import annotations


def test_download_variant_image_zip_and_raw(monkeypatch):
    # The one shared download helper both flash paths call: zip_member -> extract, else raw.
    from src.core import flash_core
    calls: dict = {}

    def _raw(url, _cache, name, _on):
        calls["raw"] = (url, name)
        return "/raw.bin"

    def _zip(url, _cache, an, mem, _on):
        calls["zip"] = (url, an, mem)
        return "/x.bin"

    monkeypatch.setattr(flash_core, "download_to", _raw)
    monkeypatch.setattr(flash_core, "download_and_extract", _zip)

    raw = flash_core.download_variant_image({"url": "u", "name": "app.bin"}, "/c", lambda _s: None)
    assert raw == "/raw.bin" and calls["raw"] == ("u", "app.bin") and "zip" not in calls

    calls.clear()
    zp = flash_core.download_variant_image(
        {"url": "z", "name": "board.bin", "zip_member": "merged.bin", "zip_name": "bundle.zip"},
        "/c", lambda _s: None)
    # zip_name (the archive filename) is preferred over name; the member is passed through
    assert zp == "/x.bin" and calls["zip"] == ("z", "bundle.zip", "merged.bin")


class _RecProfile:
    """A flash_core-style profile that records the flash_assets call so a test can check it."""
    def __init__(self, variant):
        self._variant = variant
        self.flash_kwargs: dict = {}

    def app_offset(self, _chip):
        return "0x10000"

    def latest_release(self):
        return ("tag", [self._variant])

    def default_variant(self, assets, _chip):
        return assets[0]

    def support_files(self, _chip, _cache, _cap):
        return None

    def flash_assets(self, port, chip, app_path, cap, **kw):
        self.flash_kwargs = kw
        return 0


def _run_batch(monkeypatch, tmp_path, variant):
    from src.core import batch as batch_mod
    from src.core import flash_core
    prof = _RecProfile(variant)
    monkeypatch.setattr(flash_core, "get_profile", lambda _pid: prof)
    monkeypatch.setattr(flash_core, "cache_dir", lambda: str(tmp_path))
    monkeypatch.setattr(flash_core, "_detect_chip", lambda _port, _cap: "esp32")
    monkeypatch.setattr(flash_core, "download_variant_image",
                        lambda _v, _c, _cap: str(tmp_path / "app.bin"))
    bf = batch_mod.BatchFlasher(on_line=lambda _s: None)
    res = bf.flash_sequential([batch_mod.FlashJob(port="COM3", profile_id="x")])
    return res, prof


def test_batch_honors_an_explicit_variant_offset(monkeypatch, tmp_path):
    # Was drift: batch ignored a variant's "offset" and wrote to the core default. Now it passes it,
    # exactly like FlashEngine (variant.get("offset") or core.app_offset(chip)).
    variant = {"name": "app.bin", "url": "u", "offset": "0x1000"}
    res, prof = _run_batch(monkeypatch, tmp_path, variant)
    assert res[0].success
    assert prof.flash_kwargs.get("app_offset") == "0x1000"   # the variant's offset, not the default


def test_batch_falls_back_to_the_core_offset_when_variant_has_none(monkeypatch, tmp_path):
    # No explicit offset -> app_offset resolves to profile.app_offset(chip) (0x10000) — the same
    # value FlashEngine's `variant.get("offset") or core.app_offset(chip)` yields.
    res, prof = _run_batch(monkeypatch, tmp_path, {"name": "app.bin", "url": "u"})
    assert res[0].success
    assert prof.flash_kwargs.get("app_offset") == "0x10000"
