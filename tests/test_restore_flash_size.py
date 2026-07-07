"""restore_flash — the write and verify must be symmetric so an EXACT restore verifies clean.

Regression for the false "flash may be corrupt" bug: restore_flash() wrote the dump with
`write_flash --flash_size detect`, which re-patches the flash-size nibble in the image header
IN FLASH to the chip's physically-detected size. On chips whose second-stage bootloader lives at
flash offset 0x0 (ESP32-S3 / C2 / C3 / C6 / C5 / H2 — see flash_core._BOOTLOADER_0) the write
address 0x0 IS that header, so the byte on flash then differs from backup_path. The subsequent
`verify_flash` reads its reference straight from backup_path (esptool's default --flash_size keep),
sees a 1-byte diff, and reports corruption with a non-zero rc — despite a successful, but
NON-byte-exact, restore. The restore must write with `--flash_size keep` so it reproduces the dump
verbatim AND stays symmetric with the verify. No hardware — esptool is stubbed.
"""

from __future__ import annotations

from src.core import backup


def _first(calls, cmd):
    return next(a for a in calls if cmd in a)


def test_restore_writes_with_flash_size_keep_not_detect(tmp_path, monkeypatch):
    bin_path = tmp_path / "esp32s3_backup.bin"
    # An esptool app image begins with the 0xE9 magic; byte[3] holds the flash-size nibble that
    # `--flash_size detect` would rewrite in flash on a bootloader-at-0x0 chip.
    bin_path.write_bytes(b"\xe9\x02\x02\x00" + b"\x00" * 1020)

    calls: list[list[str]] = []

    def fake_run_stream(argv, on_line):
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(backup, "_run_stream", fake_run_stream)

    rc = backup.restore_flash("COM5", str(bin_path), lambda s: None,
                              chip="esp32s3", verify=True)
    assert rc == 0

    write = _first(calls, "write_flash")
    fi = write.index("--flash_size")
    # A restore must reproduce the dump verbatim: keep, never detect.
    assert write[fi + 1] == "keep", f"restore must write with keep, got {write[fi + 1]!r}"
    assert "detect" not in write, "detect re-patches the header nibble -> non-byte-exact restore"


def test_restore_write_and_verify_use_symmetric_flash_size(tmp_path, monkeypatch):
    """Write and verify must apply the SAME header transformation, or the verify diffs on a
    bootloader@0x0 chip. verify_flash uses esptool's default --flash_size keep (no explicit flag),
    so the write must also be `keep` — a `detect` write breaks the symmetry and trips a spurious
    'flash may be corrupt' on an otherwise-successful restore."""
    bin_path = tmp_path / "esp32c3_backup.bin"
    bin_path.write_bytes(b"\xe9\x02\x02\x00" + b"\x00" * 4092)

    calls: list[list[str]] = []
    warned: list[str] = []

    def fake_run_stream(argv, on_line):
        calls.append(list(argv))
        return 0  # both the write and the verify "succeed"

    monkeypatch.setattr(backup, "_run_stream", fake_run_stream)

    rc = backup.restore_flash("COM5", str(bin_path),
                              lambda s: warned.append(s) if "corrupt" in s.lower() else None,
                              chip="esp32c3", verify=True)

    write = _first(calls, "write_flash")
    verify = _first(calls, "verify_flash")

    write_fs = write[write.index("--flash_size") + 1]
    # verify_flash carries no explicit --flash_size -> esptool default is "keep".
    verify_fs = verify[verify.index("--flash_size") + 1] if "--flash_size" in verify else "keep"
    assert write_fs == verify_fs, (
        f"write uses --flash_size {write_fs!r} but verify uses {verify_fs!r}; a mismatch trips a "
        "false corruption failure on a bootloader-at-0x0 chip")

    # With symmetric semantics a clean write+verify must NOT report corruption and returns 0.
    assert not warned, warned
    assert rc == 0
