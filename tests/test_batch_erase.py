"""BatchFlasher must FAIL the job when a requested erase is skipped or fails — never swallow it.

Regression: batch._flash_one detected the chip and, only `if chip:`, ran erase() while discarding
its return code. FlashResult.success was derived solely from the later flash rc, so a chip-detection
miss (erase silently skipped) or a nonzero erase exit (wipe failed) was recorded as a clean success
even though the requested NVS/SPIFFS/user-data wipe never happened. create_deck_flash_plan() sets
erase_first=True on every job and a merged/full write does NOT overwrite those regions, so stale data
would persist silently. Network + hardware fully mocked."""

from __future__ import annotations

from src.core import batch, flash_core


def _install_common(monkeypatch, tmp_path):
    """Wire a profile whose flash_assets always succeeds (rc 0), so ONLY the erase outcome
    can decide the job result. Returns nothing — callers patch _detect_chip / erase per case."""
    variant = {"name": "app.bin", "url": "https://github.com/x/app.bin",
               "chip": "esp32", "offset": "0x10000"}

    class FakeProfile:
        def latest_release(self):
            return ("v1", [variant])

        def default_variant(self, assets, chip):
            return variant

        def support_files(self, chip, cache, on_line):
            return None

        def flash_assets(self, *a, **k):
            return 0  # the reflash itself always "succeeds"

    monkeypatch.setattr(flash_core, "get_profile", lambda pid: FakeProfile())
    monkeypatch.setattr(flash_core, "cache_dir", lambda: str(tmp_path))
    p = tmp_path / "app.bin"
    p.write_bytes(b"bytes")
    monkeypatch.setattr(flash_core, "download_to", lambda u, c, n, cap: str(p))


def test_batch_fails_when_erase_returns_nonzero(monkeypatch, tmp_path):
    """A nonzero erase exit must fail the job (not be masked by a later successful flash)."""
    _install_common(monkeypatch, tmp_path)
    monkeypatch.setattr(flash_core, "_detect_chip", lambda port, cap: "esp32")
    erase_calls = []

    def fake_erase(port, chip, on_line):
        erase_calls.append((port, chip))
        return 1  # transient reset-into-bootloader failure

    monkeypatch.setattr(flash_core, "erase", fake_erase)

    bf = batch.BatchFlasher(on_line=lambda s: None)
    res = bf.flash_sequential([batch.FlashJob(port="COM5", profile_id="marauder", erase_first=True)])

    assert erase_calls, "erase must have been attempted"
    assert res[0].success is False, "a failed erase must fail the job, not report success"
    assert res[0].exit_code == 1
    assert "erase" in res[0].error.lower()


def test_batch_fails_when_chip_undetected_so_erase_skipped(monkeypatch, tmp_path):
    """If the chip can't be detected, the erase is skipped — the job must FAIL, not proceed.

    Simulates a CH340/CP210x board that answers chip_id intermittently: the first probe (for the
    erase) returns None. The old code silently skipped the wipe and reflashed anyway."""
    _install_common(monkeypatch, tmp_path)

    probes = []

    def flaky_detect(port, cap):
        probes.append(port)
        return None if len(probes) == 1 else "esp32"  # miss on the erase probe, hit later

    monkeypatch.setattr(flash_core, "_detect_chip", flaky_detect)

    def must_not_run(*a, **k):
        raise AssertionError("erase() must not run when the chip was not detected")

    monkeypatch.setattr(flash_core, "erase", must_not_run)

    bf = batch.BatchFlasher(on_line=lambda s: None)
    res = bf.flash_sequential([batch.FlashJob(port="COM5", profile_id="marauder", erase_first=True)])

    assert res[0].success is False, "a skipped erase must fail the job, not silently reflash"
    assert "erase" in res[0].error.lower()


def test_batch_succeeds_when_erase_succeeds(monkeypatch, tmp_path):
    """Control: a clean erase (rc 0) followed by a clean flash still reports success."""
    _install_common(monkeypatch, tmp_path)
    monkeypatch.setattr(flash_core, "_detect_chip", lambda port, cap: "esp32")
    monkeypatch.setattr(flash_core, "erase", lambda port, chip, on_line: 0)

    bf = batch.BatchFlasher(on_line=lambda s: None)
    res = bf.flash_sequential([batch.FlashJob(port="COM5", profile_id="marauder", erase_first=True)])

    assert res[0].success is True
