"""Beat 275 - download-truncation twins (cc-deep-audit-12 / hub twin S4, MED).

`os_catalog.download` and `sd_backend.download_image` streamed a body with `iter_content`, tracking
`written` against the declared `content-length` (`total`), but never checked `written == total`
the loop. A truncated stream (connection dropped mid-body) ends `iter_content` WITHOUT raising, so a
short read became a "complete" file: os_catalog returned a truncated OS image/sig/checksums to
be flashed/trusted; sd_backend `os.replace`d a truncated image into the shared cache. The sibling
`firmware_vault._safe_streamed_download` already had this guard (beat 246) -- these were the twins.

Fix: when `total` (Content-Length) is known, raise ValueError("incomplete download: ...") on
`written != total`. sd_backend raises BEFORE `os.replace`, so the truncated temp file is unlinked by
the except and never cached. When Content-Length is UNKNOWN (`total == 0`) the check is skipped (we
don't invent a size) -- the SHA-256/pinned check remains the anchor.

Discriminating (fail on buggy HEAD, pass on the fix):
  - test_os_download_truncated_raises / test_sd_download_truncated_raises: a body shorter than its
    declared Content-Length raises (HEAD returns/commits the truncated file).
  - test_sd_download_truncated_leaves_no_cache_file: no cache file survives the truncated download.
Guards (pass on both HEAD and the fix):
  - *_complete_succeeds: a full-length body succeeds.
  - test_os_download_no_content_length_succeeds: no declared size -> still succeeds (unchanged).
"""
from __future__ import annotations

import pytest

from src.core import os_catalog as oc
from src.core.backends import sd_backend as sd

_OS_URL = "https://cdimage.kali.org/x.iso"
_SD_URL = "https://github.com/o/r/releases/download/v1/pi.img"


def _resp(chunks, *, declared):
    class _R:
        is_redirect = False
        is_permanent_redirect = False
        headers = {"content-length": str(declared)} if declared is not None else {}
        closed = False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=1):
            return iter(chunks)

        def close(self):
            self.closed = True

    return _R()


def _noline(*_a, **_k):
    pass


def test_os_download_truncated_raises(tmp_path, monkeypatch):
    """os_catalog: declared 6 bytes, only 3 arrive -> must raise, not return a truncated file."""
    monkeypatch.setattr(oc.requests, "get", lambda *a, **k: _resp([b"abc"], declared=6))
    with pytest.raises(ValueError, match="incomplete"):
        oc.download(_OS_URL, str(tmp_path), _noline)


def test_os_download_complete_succeeds(tmp_path, monkeypatch):
    """Guard: a full-length os_catalog download returns the complete file."""
    monkeypatch.setattr(oc.requests, "get", lambda *a, **k: _resp([b"abc", b"def"], declared=6))
    out = oc.download(_OS_URL, str(tmp_path), _noline)
    with open(out, "rb") as fh:
        assert fh.read() == b"abcdef"


def test_os_download_no_content_length_succeeds(tmp_path, monkeypatch):
    """Guard: no declared size -> can't check completeness; behavior unchanged (SHA anchors)."""
    monkeypatch.setattr(oc.requests, "get", lambda *a, **k: _resp([b"abc"], declared=None))
    out = oc.download(_OS_URL, str(tmp_path), _noline)
    with open(out, "rb") as fh:
        assert fh.read() == b"abc"


def test_sd_download_truncated_raises(tmp_path, monkeypatch):
    """sd_backend: declared 6 bytes, only 3 arrive -> must raise, not commit a truncated image."""
    monkeypatch.setattr(sd.requests, "get", lambda *a, **k: _resp([b"abc"], declared=6))
    with pytest.raises(ValueError, match="incomplete"):
        sd.download_image(_SD_URL, str(tmp_path), _noline)


def test_sd_download_truncated_leaves_no_cache_file(tmp_path, monkeypatch):
    """sd_backend: a truncated download leaves NO cache file (temp unlinked)."""
    monkeypatch.setattr(sd.requests, "get", lambda *a, **k: _resp([b"abc"], declared=6))
    with pytest.raises(ValueError):
        sd.download_image(_SD_URL, str(tmp_path), _noline)
    assert list(tmp_path.iterdir()) == [], "truncated download must not reach the cache"


def test_sd_download_complete_succeeds(tmp_path, monkeypatch):
    """Guard: a full-length sd_backend download commits the complete image."""
    monkeypatch.setattr(sd.requests, "get", lambda *a, **k: _resp([b"abc", b"def"], declared=6))
    out = sd.download_image(_SD_URL, str(tmp_path), _noline)
    with open(out, "rb") as fh:
        assert fh.read() == b"abcdef"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
