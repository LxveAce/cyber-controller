"""Concurrency-safety tests for the shared firmware cache in ``src.core.flash_core``.

``cache_dir()`` is ONE process-wide directory and ``download_to`` / ``download_and_extract`` write
DETERMINISTIC filenames, so two concurrent flashes of the SAME firmware resolve the identical dest
path. The old ``open(dest, "wb")`` truncated that file to 0 bytes while the first flash's esptool
child was mid-read of the same path, flashing a corrupt/empty image (a full-flash collision on the
shared bootloader/partitions bricks the board) while esptool still exited 0 and the UI reported
"Flash complete". These tests pin the fix: serialize per destination path and download ONCE per
session (atomic ``os.replace``, then REUSE the completed file instead of re-truncating a path
another in-flight flash may currently be reading).
"""

from __future__ import annotations

import threading

import pytest

flash_core = pytest.importorskip("src.core.flash_core")


@pytest.fixture(autouse=True)
def _clear_cache_session():
    """Isolate the module-global download-once state around each test in this file."""
    for attr in ("_downloaded_paths", "_cache_path_locks"):
        obj = getattr(flash_core, attr, None)
        if obj is not None:
            obj.clear()
    yield
    for attr in ("_downloaded_paths", "_cache_path_locks"):
        obj = getattr(flash_core, attr, None)
        if obj is not None:
            obj.clear()


def test_download_to_downloads_once_under_concurrency(monkeypatch, tmp_path):
    """N threads flashing the SAME firmware (to different ports) must fetch the shared cache file
    ONCE and reuse it — never re-``open(dest, "wb")`` a path another in-flight flash is reading."""
    payload = b"BOOTLOADER-IMAGE" * 4096
    calls: list[str] = []
    calls_lock = threading.Lock()
    start = threading.Barrier(4)

    def fake_get(url):
        with calls_lock:
            calls.append(url)
        return payload

    monkeypatch.setattr(flash_core, "_http_get", fake_get)

    results: dict[int, str] = {}
    errors: list[BaseException] = []

    def worker(i: int):
        try:
            start.wait()  # release all threads together to force contention
            results[i] = flash_core.download_to(
                "https://github.com/x/y/esp32_bootloader.bin",
                str(tmp_path), "esp32_bootloader.bin", lambda _s: None)
        except BaseException as exc:  # pragma: no cover - surfaced via the assert below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent download raised: {errors!r}"
    # Fetched exactly once; the other three flashes reused the completed file (no re-truncation).
    assert len(calls) == 1
    dest = tmp_path / "esp32_bootloader.bin"
    assert set(results.values()) == {str(dest)}
    # The cached image is byte-complete (never a truncated 0-byte / partial window).
    assert dest.read_bytes() == payload


def test_download_to_reuse_does_not_retruncate(monkeypatch, tmp_path):
    """A completed cache file that another flash may currently be READING must not be re-truncated
    by a second flash of the same firmware — the second flash reuses it verbatim."""
    monkeypatch.setattr(flash_core, "_http_get", lambda url: b"REAL-IMAGE")

    dest = flash_core.download_to(
        "https://github.com/x/y/fw.bin", str(tmp_path), "fw.bin", lambda _s: None)

    # Stand in for "the exact bytes on disk that the first board's esptool is mid-read of": if the
    # second flash re-opens dest "wb" it truncates+overwrites these, corrupting the in-flight read.
    with open(dest, "wb") as f:
        f.write(b"READER-HOLDS-THIS")

    dest2 = flash_core.download_to(
        "https://github.com/x/y/fw.bin", str(tmp_path), "fw.bin", lambda _s: None)

    assert dest2 == dest
    assert (tmp_path / "fw.bin").read_bytes() == b"READER-HOLDS-THIS"


def test_download_and_extract_reuse_does_not_retruncate_member(monkeypatch, tmp_path):
    """The extracted member is the file esptool flashes; a second concurrent flash of the same
    firmware must reuse it, not re-extract+truncate a path another flash may be reading."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("merged.bin", b"MERGED-FW")
    monkeypatch.setattr(flash_core, "_http_get", lambda url: buf.getvalue())

    out = flash_core.download_and_extract(
        "https://github.com/x/y/bundle.zip", str(tmp_path), "bundle.zip", "merged.bin",
        lambda _s: None)

    # Stand in for the member bytes a concurrent flash's esptool is mid-read of.
    with open(out, "wb") as f:
        f.write(b"READER-HOLDS-THIS")

    out2 = flash_core.download_and_extract(
        "https://github.com/x/y/bundle.zip", str(tmp_path), "bundle.zip", "merged.bin",
        lambda _s: None)

    assert out2 == out
    with open(out, "rb") as f:
        assert f.read() == b"READER-HOLDS-THIS"  # reused, not re-extracted/re-truncated
