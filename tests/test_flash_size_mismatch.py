"""FLASH-MERGED-4MB regression tests.

A merged-single-bin firmware carries its own bootloader whose ESP image header declares the
flash size. `write_flash --flash_size detect` only patches the header at the WRITE offset, not a
bootloader deeper inside the blob (0x1000 on classic ESP32), so a merged build for a 16MB board
writes+verifies fine yet boot-loops a 4MB board. These lock the honest-warning detection.

Empirically grounded: the real Bruce-Awok-Mini.bin (flashed onto a 4MB COM19 ESP32) has magic
0xE9 with flash-size nibble 0x4 (=16MB) at offset 0x1000; esptool v5.3.0 prints the board's real
size as "Detected flash size: 4MB".
"""
import glob
import os

import pytest

from src.core import flash_core


def _merged(size_code: int, *, at_zero: bool = False) -> bytes:
    """A minimal merged blob with a bootloader ESP image header (magic 0xE9; byte3 high nibble =
    flash-size code, low nibble = an arbitrary freq code) at 0x0 (S3-style) or 0x1000 (classic)."""
    header = bytes([0xE9, 0x02, 0x02, (size_code << 4) | 0x0F])
    if at_zero:
        return header + b"\x00" * 64
    return b"\xFF" * 0x1000 + header + b"\x00" * 64


# ── declared_flash_size_mb ────────────────────────────────────────────────────
def test_declared_16mb_classic_esp32_is_the_bruce_bug():
    assert flash_core.declared_flash_size_mb(_merged(0x4), "esp32") == 16


def test_declared_4mb_classic_esp32():
    assert flash_core.declared_flash_size_mb(_merged(0x2), "esp32") == 4


def test_declared_reads_bootloader_at_zero_for_s3():
    assert flash_core.declared_flash_size_mb(_merged(0x4, at_zero=True), "esp32s3") == 16


def test_declared_none_without_magic():
    # a 0x1004-byte blob of zeros: no 0xE9 magic at the bootloader offset
    assert flash_core.declared_flash_size_mb(b"\x00" * 0x1004, "esp32") is None


def test_declared_none_when_too_short():
    assert flash_core.declared_flash_size_mb(b"\xE9\x00\x00", "esp32") is None


def test_declared_matches_real_bruce_image_if_cached():
    hits = glob.glob(os.path.join(flash_core.cache_dir(), "**", "Bruce-Awok-Mini.bin"), recursive=True)
    if not hits:
        pytest.skip("Bruce merged image not in cache")
    with open(hits[0], "rb") as f:
        assert flash_core.declared_flash_size_mb(f.read(0x2004), "esp32") == 16


# ── parse_detected_flash_mb ───────────────────────────────────────────────────
def test_parse_detected_flash_real_esptool_lines():
    assert flash_core.parse_detected_flash_mb("Detected flash size: 4MB") == 4
    assert flash_core.parse_detected_flash_mb("   Detected flash size: 16MB") == 16


def test_parse_detected_flash_ignores_other_lines():
    assert flash_core.parse_detected_flash_mb("Connecting....") is None
    assert flash_core.parse_detected_flash_mb("") is None


# ── flash_size_mismatch_warning ───────────────────────────────────────────────
def test_warning_fires_when_image_needs_more_than_board_has():
    w = flash_core.flash_size_mismatch_warning(16, 4)
    assert w is not None
    assert "16MB" in w and "4MB" in w and "NOT BOOT" in w


def test_no_warning_when_sizes_match():
    assert flash_core.flash_size_mismatch_warning(4, 4) is None


def test_no_warning_when_image_smaller_than_board():
    assert flash_core.flash_size_mismatch_warning(4, 16) is None


def test_no_warning_when_either_size_unknown():
    assert flash_core.flash_size_mismatch_warning(None, 4) is None
    assert flash_core.flash_size_mismatch_warning(16, None) is None
    assert flash_core.flash_size_mismatch_warning(None, None) is None


# ── flash_engine glue (covers BOTH the download and local_path merged flash paths) ────────────
from src.core.flash_engine import FlashEngine  # noqa: E402


def test_engine_warns_on_mismatch_and_does_not_say_flash_complete():
    """The shared glue used by both flash paths: a merged image demanding 16MB on a 4MB board
    (esptool prints "Detected flash size: 4MB" during the write) warns and drops "Flash complete"."""
    eng = FlashEngine()
    lines: list[str] = []
    progress: list[tuple[int, str]] = []

    def run(capture):
        capture("Detected flash size: 4MB")  # what esptool prints mid-write
        return 0

    ret = eng._flash_with_size_warning(16, lines.append, lambda p, m: progress.append((p, m)), run)
    assert ret is True  # the write itself succeeded
    assert any("FLASH-SIZE MISMATCH" in ln for ln in lines)
    assert progress and "likely won't boot" in progress[-1][1]
    assert not any(msg == "Flash complete" for _, msg in progress)


def test_engine_clean_complete_when_sizes_match():
    eng = FlashEngine()
    progress: list[tuple[int, str]] = []

    def run(capture):
        capture("Detected flash size: 16MB")
        return 0

    ret = eng._flash_with_size_warning(16, lambda ln: None, lambda p, m: progress.append((p, m)), run)
    assert ret is True
    assert progress[-1] == (100, "Flash complete")


def test_engine_no_warning_when_declared_unknown():
    eng = FlashEngine()
    progress: list[tuple[int, str]] = []

    def run(capture):
        capture("Detected flash size: 4MB")
        return 0

    ret = eng._flash_with_size_warning(None, lambda ln: None, lambda p, m: progress.append((p, m)), run)
    assert ret is True
    assert progress[-1] == (100, "Flash complete")


def test_engine_reports_failure_when_flash_fails():
    eng = FlashEngine()
    progress: list[tuple[int, str]] = []
    ret = eng._flash_with_size_warning(16, lambda ln: None, lambda p, m: progress.append((p, m)),
                                       lambda capture: 2)
    assert ret is False
    assert progress[-1] == (0, "Flash failed")


def test_declared_merged_size_reads_a_local_merged_bin(tmp_path):
    """The local_path path: a lone .bin is image_model=MERGED, so a 16MB-built local .bin is caught."""
    p = tmp_path / "local.bin"
    p.write_bytes(b"\xFF" * 0x1000 + bytes([0xE9, 0x02, 0x02, 0x4F]) + b"\x00" * 64)
    eng = FlashEngine()
    assert eng._declared_merged_size(flash_core.IMAGE_MERGED, str(p), "esp32") == 16


def test_declared_merged_size_none_for_multifile_and_missing():
    eng = FlashEngine()
    assert eng._declared_merged_size(flash_core.IMAGE_MULTI, __file__, "esp32") is None
    assert eng._declared_merged_size(flash_core.IMAGE_MERGED, "does_not_exist_xyz.bin", "esp32") is None


import types  # noqa: E402


def test_offline_vault_fallback_warns_on_size_mismatch(tmp_path, monkeypatch):
    """The offline-vault fallback path (the 3rd merged-flash path) must ALSO warn on a size mismatch,
    not report a clean 'Flash complete (offline vault)' on a board it will bootloop. (Found by the
    flash bug-hunt: the shipped FLASH-MERGED-4MB fix had wired download + local_path but not this one.)"""
    blob = tmp_path / "cached.bin"
    blob.write_bytes(b"\xFF" * 0x1000 + bytes([0xE9, 0x02, 0x02, 0x4F]) + b"\x00" * 64)  # 16MB header

    class FakeCustom:
        image_model = flash_core.IMAGE_MERGED

        def flash_local(self, port, chip, path, on_line, baud=115200, extra_args=None):
            on_line("Detected flash size: 4MB")  # what esptool prints for this 4MB board
            on_line("Hash of data verified.")
            return 0

    monkeypatch.setattr(flash_core, "get_profile", lambda k: FakeCustom())
    eng = FlashEngine()
    prof = types.SimpleNamespace(offline_fallback_path=str(blob), baud=115200)
    lines: list[str] = []
    progress: list[tuple[int, str]] = []
    ret = eng._flash_offline_fallback("COM_TEST", "esp32", prof,
                                      lines.append, lambda p, m: progress.append((p, m)), "offline")
    assert ret is True  # the write itself succeeded
    assert any("FLASH-SIZE MISMATCH" in ln for ln in lines)
    # must NOT claim a clean offline-vault success on a board that will bootloop
    assert not any(msg == "Flash complete (offline vault)" for _, msg in progress)
    assert progress and "likely won't boot" in progress[-1][1]


def test_offline_vault_fallback_clean_when_sizes_match(tmp_path, monkeypatch):
    """A matching-size cached image still reports the normal offline-vault success (no false warning)."""
    blob = tmp_path / "cached.bin"
    blob.write_bytes(b"\xFF" * 0x1000 + bytes([0xE9, 0x02, 0x02, 0x2F]) + b"\x00" * 64)  # 4MB header

    class FakeCustom:
        image_model = flash_core.IMAGE_MERGED

        def flash_local(self, port, chip, path, on_line, baud=115200, extra_args=None):
            on_line("Detected flash size: 4MB")
            return 0

    monkeypatch.setattr(flash_core, "get_profile", lambda k: FakeCustom())
    eng = FlashEngine()
    prof = types.SimpleNamespace(offline_fallback_path=str(blob), baud=115200)
    progress: list[tuple[int, str]] = []
    ret = eng._flash_offline_fallback("COM_TEST", "esp32", prof,
                                      lambda ln: None, lambda p, m: progress.append((p, m)), "offline")
    assert ret is True
    assert progress[-1] == (100, "Flash complete (offline vault)")
