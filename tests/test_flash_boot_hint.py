"""_run_stream appends a hold-BOOT recovery hint when — and only when — an esptool run fails because the
chip never entered download mode. That "Wrong boot mode / Failed to connect" case is the single most common
flashing snag on CP210x/CH340 boards that don't auto-reset, and the raw esptool error doesn't say what to do.

Hermetic: drives _run_stream with a plain Python subprocess (no esptool, no serial, no hardware).
"""

from __future__ import annotations

import sys

import pytest

flash_core = pytest.importorskip("src.core.flash_core")


def _run(argv):
    lines: list[str] = []
    rc = flash_core._run_stream(argv, lines.append)
    return rc, lines


def _has_hint(lines):
    return any("[hint]" in ln and "boot" in ln.lower() for ln in lines)


def test_hint_on_download_mode_failure():
    argv = [sys.executable, "-c",
            "import sys; print('A fatal error occurred: Failed to connect to ESP32: "
            "Wrong boot mode detected (0x13); perhaps a reset is needed?'); sys.exit(2)"]
    rc, lines = _run(argv)
    assert rc == 2
    assert _has_hint(lines), f"expected a hold-BOOT hint, got: {lines}"


def test_no_hint_on_success():
    argv = [sys.executable, "-c", "print('Hash of data verified.')"]
    rc, lines = _run(argv)
    assert rc == 0
    assert not _has_hint(lines)


def test_no_hint_on_unrelated_failure():
    # A verify/flash-size failure is NOT a download-mode problem — the hint would be misleading noise.
    argv = [sys.executable, "-c",
            "import sys; print('A fatal error occurred: MD5 of file does not match data in flash!'); sys.exit(2)"]
    rc, lines = _run(argv)
    assert rc == 2
    assert not _has_hint(lines)
