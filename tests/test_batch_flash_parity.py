"""BatchFlasher delegates to the ONE flash path (FlashEngine) — no hand-maintained copy.

Atlas's (b) ruling: `BatchFlasher._flash_one` now routes through `FlashEngine.flash` /
`_flash_esptool`, so erase + variant + extra_args + `strip_reserved_extra_args` + offset + the tails
all come from the single proven path, and a batch flash is byte-identical to a single flash of the
same profile. These tests assert the delegation + that the brick-adjacent `extra_args` (and core_id)
ride through UNTOUCHED, mocking `FlashEngine.flash` so no esptool/network runs. The per-firmware
flash semantics (erase-fail, sha256, zip extraction) are covered at their own level —
`test_flash_engine_*` + `test_flash_core` + `test_download_variant_image_zip_and_raw` below.
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
    assert zp == "/x.bin" and calls["zip"] == ("z", "bundle.zip", "merged.bin")


def _spy_engine(monkeypatch, ok=True):
    """Stub `FirmwareProfile.from_file` -> a fixed engine profile (with extra_args, to prove it
    rides through) and `FlashEngine.flash` -> a spy recording (port, profile), returning *ok*.
    Returns (calls, profile)."""
    from src.core.flash_engine import FirmwareProfile, FlashEngine
    prof = FirmwareProfile(id="marauder", core_id="marauder", chip="esp32",
                           extra_args=["--after", "no_reset"], flash_mode="app")
    monkeypatch.setattr(FirmwareProfile, "from_file", classmethod(lambda cls, path: prof))
    calls = []

    def fake_flash(self, port, profile, progress=None):
        calls.append((port, profile))
        if progress:
            progress(100, "flash complete")
        return ok

    monkeypatch.setattr(FlashEngine, "flash", fake_flash)
    return calls, prof


def test_batch_delegates_to_the_one_flash_path(monkeypatch):
    from src.core import batch as batch_mod
    calls, prof = _spy_engine(monkeypatch, ok=True)
    bf = batch_mod.BatchFlasher(on_line=lambda _s: None)
    res = bf.flash_sequential([batch_mod.FlashJob(port="COM3", profile_id="marauder")])
    assert len(calls) == 1                              # routed through the ONE FlashEngine.flash
    port, passed = calls[0]
    assert port == "COM3" and passed is prof            # the engine profile, not a hand-built one
    assert res[0].success is True and res[0].exit_code == 0


def test_job_options_and_extra_args_flow_through(monkeypatch):
    # erase/mode/variant ride onto the engine profile (as a single flash sets them from the UI); the
    # brick-adjacent extra_args + core_id ride UNTOUCHED -> byte-identical to a single flash.
    from src.core import batch as batch_mod
    calls, _prof = _spy_engine(monkeypatch, ok=True)
    bf = batch_mod.BatchFlasher(on_line=lambda _s: None)
    bf.flash_sequential([batch_mod.FlashJob(port="COM3", profile_id="marauder",
                                            erase_first=True, mode="full", variant_name="cyd")])
    _, passed = calls[0]
    assert passed.erase_first is True and passed.flash_mode == "full" and passed.variant == "cyd"
    assert passed.extra_args == ["--after", "no_reset"]   # NOT stripped by batch (the old gap)
    assert passed.core_id == "marauder"


def test_a_flashengine_failure_fails_the_job(monkeypatch):
    from src.core import batch as batch_mod
    _spy_engine(monkeypatch, ok=False)
    bf = batch_mod.BatchFlasher(on_line=lambda _s: None)
    res = bf.flash_sequential([batch_mod.FlashJob(port="COM3", profile_id="marauder")])
    assert res[0].success is False and res[0].exit_code == 1


def test_a_load_error_fails_only_that_job(monkeypatch):
    # A bad profile id / unreadable profile must fail that ONE job, not abort the batch.
    from src.core import batch as batch_mod
    from src.core.flash_engine import FirmwareProfile
    def boom(cls, p):
        raise FileNotFoundError("nope")
    monkeypatch.setattr(FirmwareProfile, "from_file", classmethod(boom))
    bf = batch_mod.BatchFlasher(on_line=lambda _s: None)
    res = bf.flash_sequential([batch_mod.FlashJob(port="COM3", profile_id="does-not-exist")])
    assert len(res) == 1 and res[0].success is False and res[0].error
