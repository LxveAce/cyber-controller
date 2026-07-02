"""FlashEngine.backup — must read the chip's REAL flash size, not a hardcoded 4 MB.

Regression for the truncated-backup data-loss bug: a fixed 0x400000 read silently produced a partial
recovery image on any >4 MB board (S3 DevKit 8 MB, T-Deck 16 MB, …). No hardware — esptool is stubbed."""

from __future__ import annotations

from src.core import flash_core
from src.core.flash_engine import FlashEngine


def test_engine_backup_reads_full_detected_size(monkeypatch, tmp_path):
    monkeypatch.setattr(flash_core, "detect_chip", lambda port, on_line: "esp32s3")

    captured = {}

    def fake_run_stream(argv, on_line):
        if "flash_id" in argv:
            on_line("Detected flash size: 8MB")
            return 0
        if "read_flash" in argv:
            captured["argv"] = list(argv)
            return 0
        return 0

    monkeypatch.setattr(flash_core, "_run_stream", fake_run_stream)
    assert FlashEngine().backup("COM5", tmp_path / "bk.bin")
    argv = captured["argv"]
    i = argv.index("read_flash")
    # argv = [..., "read_flash", "0x0", <size>, <out>]
    assert argv[i + 2] == "0x800000", f"expected a full 8 MB read, got {argv[i + 2]}"


def test_engine_backup_falls_back_to_4mb_when_undetected(monkeypatch, tmp_path):
    monkeypatch.setattr(flash_core, "detect_chip", lambda port, on_line: "esp32")

    captured = {}

    def fake_run_stream(argv, on_line):
        if "read_flash" in argv:
            captured["argv"] = list(argv)
        return 0  # flash_id emits nothing parseable

    monkeypatch.setattr(flash_core, "_run_stream", fake_run_stream)
    assert FlashEngine().backup("COM5", tmp_path / "bk.bin")
    a = captured["argv"]
    i = a.index("read_flash")
    assert a[i + 2] == "0x400000"  # no regression for undetectable / 4 MB boards


def test_engine_backup_warns_loudly_when_size_undetected(monkeypatch, tmp_path):
    """A detection MISS must be loud: the completion status carries a truncation caveat instead of a
    clean "Backup complete" — a silent 4 MB read of a larger board is the data-loss this path guards."""
    monkeypatch.setattr(flash_core, "detect_chip", lambda port, on_line: "esp32")
    monkeypatch.setattr(flash_core, "_run_stream", lambda argv, on_line: 0)  # flash_id emits nothing

    statuses: list[str] = []
    ok = FlashEngine().backup("COM5", tmp_path / "bk.bin",
                              progress_callback=lambda pct, msg: statuses.append(msg))
    assert ok
    assert any("truncated" in s.lower() for s in statuses), statuses
    assert not any(s == "Backup complete" for s in statuses)  # must NOT read as a clean full backup


def test_engine_backup_honors_explicit_size(monkeypatch, tmp_path):
    """An explicit size still wins (no detection call needed)."""
    monkeypatch.setattr(flash_core, "detect_chip", lambda port, on_line: "esp32")
    captured = {}

    def fake_run_stream(argv, on_line):
        if "flash_id" in argv:
            raise AssertionError("must not detect when size is explicit")
        if "read_flash" in argv:
            captured["argv"] = list(argv)
        return 0

    monkeypatch.setattr(flash_core, "_run_stream", fake_run_stream)
    assert FlashEngine().backup("COM5", tmp_path / "bk.bin", size="0x200000")
    a = captured["argv"]
    i = a.index("read_flash")
    assert a[i + 2] == "0x200000"
