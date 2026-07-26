"""Batch erase behaviour, at the delegation level.

BatchFlasher._flash_one now delegates to FlashEngine (Atlas's (b) ruling — one flash path, no
hand-copy). So batch no longer runs its own erase; it sets `erase_first` on the engine profile and
FlashEngine honours it. This asserts (a) a job's `erase_first` rides onto the engine profile, and
(b) a FlashEngine failure (an erase that fails aborts the flash there) fails the job. FlashEngine's
own erase-before-write + erase-fail-aborts semantics are covered by `test_flash_engine_erase_first`.
"""
from __future__ import annotations


def _spy(monkeypatch, ok):
    from src.core.flash_engine import FirmwareProfile, FlashEngine
    prof = FirmwareProfile(id="marauder", core_id="marauder", chip="esp32")
    monkeypatch.setattr(FirmwareProfile, "from_file", classmethod(lambda cls, p: prof))
    seen = []
    monkeypatch.setattr(FlashEngine, "flash",
                        lambda self, port, profile, progress=None: (seen.append(profile) or ok))
    return seen


def test_job_erase_first_rides_onto_the_engine_profile(monkeypatch):
    from src.core import batch
    seen = _spy(monkeypatch, ok=True)
    bf = batch.BatchFlasher(on_line=lambda _s: None)
    bf.flash_sequential([batch.FlashJob(port="COM3", profile_id="marauder", erase_first=True)])
    assert seen and seen[0].erase_first is True   # FlashEngine (which honours it) gets erase_first


def test_no_erase_requested_leaves_it_off(monkeypatch):
    from src.core import batch
    seen = _spy(monkeypatch, ok=True)
    bf = batch.BatchFlasher(on_line=lambda _s: None)
    bf.flash_sequential([batch.FlashJob(port="COM3", profile_id="marauder")])
    assert seen and seen[0].erase_first is False


def test_a_failed_flash_including_a_failed_erase_fails_the_job(monkeypatch):
    # FlashEngine returns False when an erase-first fails (it aborts rather than reflash over stale
    # flash); batch must surface that as a failed job, never a silent success.
    from src.core import batch
    _spy(monkeypatch, ok=False)
    bf = batch.BatchFlasher(on_line=lambda _s: None)
    res = bf.flash_sequential(
        [batch.FlashJob(port="COM3", profile_id="marauder", erase_first=True)])
    assert res[0].success is False and res[0].exit_code == 1


def test_a_successful_flash_succeeds(monkeypatch):
    from src.core import batch
    _spy(monkeypatch, ok=True)
    bf = batch.BatchFlasher(on_line=lambda _s: None)
    res = bf.flash_sequential(
        [batch.FlashJob(port="COM3", profile_id="marauder", erase_first=True)])
    assert res[0].success is True and res[0].exit_code == 0
