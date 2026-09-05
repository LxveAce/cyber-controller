"""F03 caveat: the backup read must honor the operator's Flash Baud (an explicit override) and else
fall back to the 921600 default — not hardcode 921600 regardless of the setting. Mocked esptool
runner; no serial, no real read."""

from __future__ import annotations

from src.core import flash_core
from src.core.flash_engine import FlashEngine


def _capture_backup_argv(monkeypatch, *, baud):
    seen = {}

    def fake_run_stream(argv, on_line):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(flash_core, "_run_stream", fake_run_stream)
    engine = FlashEngine()
    # size given, so _detect_flash_size isn't reached; chip given, so detect_chip isn't reached.
    monkeypatch.setattr(engine, "_write_backup_meta", lambda *a, **k: None)
    ok = engine.backup("COM7", "out.bin", chip="esp32", size="4MB", baud=baud)
    assert ok is True
    return seen["argv"]


def test_backup_uses_the_passed_baud(monkeypatch):
    argv = _capture_backup_argv(monkeypatch, baud=115200)
    i = argv.index("--baud")
    assert argv[i + 1] == "115200"   # the explicit override reached the read, not a hardcoded value


def test_backup_defaults_to_921600(monkeypatch):
    # backup() defaults baud to 921600 when the caller passes nothing — prior behavior preserved.
    seen = {}
    monkeypatch.setattr(flash_core, "_run_stream",
                        lambda argv, on_line: seen.setdefault("argv", argv) or 0)
    engine = FlashEngine()
    monkeypatch.setattr(engine, "_write_backup_meta", lambda *a, **k: None)
    engine.backup("COM7", "out.bin", chip="esp32", size="4MB")
    argv = seen["argv"]
    assert argv[argv.index("--baud") + 1] == "921600"
