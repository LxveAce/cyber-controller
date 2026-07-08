"""The frozen `--_run-esptool` dispatcher must survive sys.stdout/stderr = None.

Regression for the owner's flash crash: esptool writes progress with print(); a PyInstaller --noconsole
(windowed) build has sys.stdout/stderr = None, so that print raised `OSError: [Errno 22] Invalid argument`
and crashed the flash before it could report the real connect result. src.app.main's dispatcher now rebinds
valid streams onto fd 1/2 before calling esptool. flash_core routes esptool through this dispatcher only in a
frozen build, so this path is otherwise never exercised by the suite.
"""

from __future__ import annotations

import sys
import types


def _fake_esptool(monkeypatch, recorder=None):
    """esptool stand-in doing what the real one does at the crash point: print progress to stdout+stderr."""
    def _main(argv):
        if recorder is not None:
            recorder["argv"] = argv
            recorder["stdout_none"] = sys.stdout is None
        print(".", end="", flush=True)   # the exact call that raised OSError [Errno 22]
        print("Chip is ESP32")
        sys.stderr.write("connecting...\n")
        sys.stderr.flush()
        return 0
    fake = types.ModuleType("esptool")
    fake.main = _main
    monkeypatch.setitem(sys.modules, "esptool", fake)


def test_dispatcher_survives_none_stdout(monkeypatch):
    import src.app as app
    rec = {}
    _fake_esptool(monkeypatch, rec)
    saved = (sys.stdout, sys.stderr)
    try:
        sys.stdout = None   # windowed frozen build: no console
        sys.stderr = None
        rc = app.main(["--_run-esptool", "write_flash", "0x0", "fw.bin"])
    finally:
        sys.stdout, sys.stderr = saved
    assert rc == 0
    assert rec["argv"] == ["write_flash", "0x0", "fw.bin"]
    # esptool saw a usable stream, not None -> its progress print() could not have crashed.
    assert rec["stdout_none"] is False


def test_dispatcher_leaves_esptool_argv_intact(monkeypatch):
    """The dispatcher must forward exactly the args after --_run-esptool (no stream fix-up side effects)."""
    import src.app as app
    rec = {}
    _fake_esptool(monkeypatch, rec)
    saved = (sys.stdout, sys.stderr)
    try:
        rc = app.main(["--_run-esptool", "--chip", "esp32", "--port", "COM7", "chip_id"])
    finally:
        sys.stdout, sys.stderr = saved
    assert rc == 0
    assert rec["argv"] == ["--chip", "esp32", "--port", "COM7", "chip_id"]
