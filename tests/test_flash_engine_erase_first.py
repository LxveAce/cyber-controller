"""FlashEngine single-flash path must HONOR profile.erase_first — never silently skip it.

Regression: FirmwareProfile.from_file parses ``erase_first`` from the profile JSON, but no
FlashEngine backend ever read it (only BatchFlasher honored its own independent FlashJob.erase_first).
So a single flash via ``FlashEngine.flash(port, profile)`` — the UI single-flash path — reflashed the
board WITHOUT the requested wipe, leaving stale NVS/SPIFFS/credential data behind while still reporting
"Flash complete". This mirrors the guard BatchFlasher._flash_one already enforces: a requested wipe that
FAILS must abort the flash, not be masked by a later successful write. Network + esptool fully mocked."""

from __future__ import annotations

import pytest

flash_engine = pytest.importorskip("src.core.flash_engine")
from src.core import flash_core  # noqa: E402
from src.core.flash_engine import FirmwareProfile, FlashEngine  # noqa: E402


def _local_profile(tmp_path, **over) -> FirmwareProfile:
    """A minimal esptool profile that flashes an explicit local .bin (the simplest esptool
    sub-path — no release fetch/download needed). Chip is pinned so no chip-detect runs."""
    p = tmp_path / "app.bin"
    p.write_bytes(b"\x00" * 16)
    kw = dict(backend="esptool", chip="esp32", local_path=str(p), erase_first=True)
    kw.update(over)
    return FirmwareProfile(**kw)


def test_erase_first_runs_before_the_flash(monkeypatch, tmp_path):
    """erase_first=True must run a full erase BEFORE the write, then flash (order matters)."""
    order: list[str] = []

    def fake_erase(port, chip, on_line):
        order.append("erase")
        return 0

    class FakeCustom:
        def flash_local(self, port, chip, app_path, on_line, app_offset="0x0", baud=921600,
                        support=None, flash_freq=None, extra_args=None):
            order.append("flash")
            return 0

    monkeypatch.setattr(flash_core, "erase", fake_erase)
    monkeypatch.setattr(flash_core, "get_profile", lambda pid: FakeCustom())

    ok = FlashEngine().flash("COM5", _local_profile(tmp_path, erase_first=True))

    assert ok is True
    assert order == ["erase", "flash"], "erase must run, and BEFORE the flash write"


def test_failed_erase_aborts_flash_and_reports_failure(monkeypatch, tmp_path):
    """A requested wipe that FAILS (rc != 0) must abort — the flash must NOT run and the op
    must report failure (never a masked 'Flash complete' over stale flash)."""
    flash_ran: list[bool] = []

    def fake_erase(port, chip, on_line):
        return 1  # transient reset-into-bootloader failure

    class FakeCustom:
        def flash_local(self, *a, **k):
            flash_ran.append(True)
            return 0

    monkeypatch.setattr(flash_core, "erase", fake_erase)
    monkeypatch.setattr(flash_core, "get_profile", lambda pid: FakeCustom())

    ok = FlashEngine().flash("COM5", _local_profile(tmp_path, erase_first=True))

    assert ok is False, "a failed erase must fail the flash, not report success"
    assert not flash_ran, "the flash must NOT run after a failed erase"


def test_no_erase_when_flag_is_false(monkeypatch, tmp_path):
    """Control: erase_first=False must NOT erase (the flag gates the wipe, nothing else does)."""
    erased: list[bool] = []

    def fake_erase(port, chip, on_line):
        erased.append(True)
        return 0

    class FakeCustom:
        def flash_local(self, *a, **k):
            return 0

    monkeypatch.setattr(flash_core, "erase", fake_erase)
    monkeypatch.setattr(flash_core, "get_profile", lambda pid: FakeCustom())

    ok = FlashEngine().flash("COM5", _local_profile(tmp_path, erase_first=False))

    assert ok is True
    assert not erased, "erase must NOT run when erase_first is False"
