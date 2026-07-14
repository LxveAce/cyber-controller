"""Regression guards for cc-deep-audit-3 V1 (2026-07-13): firmware_vault non-atomic download.

The vault used to stream the HTTP body DIRECTLY over the destination path and never check the
received byte count against Content-Length. Two real failure modes:

  * A silently-truncated stream (server/proxy closes the connection early WITHOUT raising) returned
    "success"; with no SHA pin (the TOFU / moving-"latest" case) the short file was hashed and
    INDEXED as valid → a later offline flash writes a truncated firmware and bricks the board.
  * `open("wb")` truncates at once, so a re-download over an existing GOOD cached .bin that is then
    interrupted DESTROYS the good copy while the index still marks it valid.

Fix: `_safe_streamed_download` now rejects a short read (downloaded != Content-Length), and
`download_firmware` streams to a `.part` temp, verifies, then `os.replace`s it into place only after
all checks pass — leaving any existing dest untouched on failure. These tests pin both.

Pure logic: the network + GitHub API + profile loading are monkeypatched; no socket is opened.
"""
from __future__ import annotations

import pytest

fwv = pytest.importorskip("src.core.firmware_vault")

_GH = "https://github.com/o/r/releases/latest"
_DL = "https://github.com/o/r/releases/download/v1/firmware.bin"


# ── _safe_streamed_download completeness check ────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, body: bytes, content_length, status: int = 200) -> None:
        self._body = body
        self.status_code = status
        self.headers: dict[str, str] = {}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)

    def iter_content(self, chunk_size: int = 1):
        for i in range(0, len(self._body), chunk_size or 1):
            yield self._body[i:i + (chunk_size or 1)]

    def raise_for_status(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def _no_ssrf(monkeypatch):
    monkeypatch.setattr(fwv, "_require_allowed_url", lambda *_a, **_k: None)


def test_truncated_stream_is_rejected(tmp_path, monkeypatch, _no_ssrf):
    """A body shorter than the declared Content-Length must RAISE, not silently succeed."""
    monkeypatch.setattr(fwv.requests, "get",
                        lambda *_a, **_k: _FakeResp(b"12345", content_length=10))
    with pytest.raises(ValueError, match="incomplete download"):
        fwv._safe_streamed_download(_DL, tmp_path / "x.bin", None, "x.bin")


def test_complete_stream_returns_full_count(tmp_path, monkeypatch, _no_ssrf):
    dest = tmp_path / "x.bin"
    monkeypatch.setattr(fwv.requests, "get",
                        lambda *_a, **_k: _FakeResp(b"1234567890", content_length=10))
    assert fwv._safe_streamed_download(_DL, dest, None, "x.bin") == 10
    assert dest.read_bytes() == b"1234567890"


def test_no_content_length_cannot_enforce_completeness(tmp_path, monkeypatch, _no_ssrf):
    """Without a declared length we can't detect truncation — it must still write what it got (the
    SHA pin is the backstop there). Documents the boundary of the completeness check."""
    dest = tmp_path / "x.bin"
    monkeypatch.setattr(fwv.requests, "get",
                        lambda *_a, **_k: _FakeResp(b"abc", content_length=None))
    assert fwv._safe_streamed_download(_DL, dest, None, "x.bin") == 3
    assert dest.read_bytes() == b"abc"


# ── download_firmware atomic promotion ────────────────────────────────────────────────────────────

@pytest.fixture
def vault(tmp_path):
    return fwv.FirmwareVault(vault_dir=tmp_path)


def _wire_merged_profile(vault, monkeypatch, *, sha_pin=None):
    """Point the vault at a merged-single-bin profile with one .bin asset, network stubbed out."""
    monkeypatch.setattr(fwv, "_safe_api_get_json", lambda _url: {
        "tag_name": "v1",
        "assets": [{"name": "firmware.bin", "browser_download_url": _DL}],
    })
    profile: dict = {"firmware_urls": {"latest": _GH}, "image_model": "merged-single-bin"}
    if sha_pin is not None:
        profile["firmware_sha256"] = {"latest": sha_pin}
    monkeypatch.setattr(vault, "_load_profile", lambda _pid: profile)


def _good_download(body: bytes):
    def _dl(_url, dest, _cb, _name):
        dest.write_bytes(body)            # writes to the .part temp it is handed
        return len(body)
    return _dl


def test_successful_download_promotes_and_indexes(vault, monkeypatch):
    _wire_merged_profile(vault, monkeypatch)
    monkeypatch.setattr(fwv, "_safe_streamed_download", _good_download(b"GOODFIRMWARE"))

    p = vault.download_firmware("bruce")
    assert p is not None and p.exists()
    assert p.read_bytes() == b"GOODFIRMWARE"
    assert not p.with_name(p.name + ".part").exists()   # temp was promoted, not left behind
    assert vault.get_cached("bruce") == p               # indexed as valid


def test_failed_redownload_does_not_clobber_existing_good_copy(vault, monkeypatch):
    """THE core guarantee: an interrupted/truncated re-download must not destroy the cached firmware
    that already flashed fine — the old bare open('wb') truncated it the instant a retry started."""
    _wire_merged_profile(vault, monkeypatch)
    monkeypatch.setattr(fwv, "_safe_streamed_download", _good_download(b"GOODFIRMWARE"))
    p1 = vault.download_firmware("bruce")
    assert p1.read_bytes() == b"GOODFIRMWARE"

    def _truncated(_url, dest, _cb, _name):
        dest.write_bytes(b"TRUNC")        # a partial lands in the temp...
        raise ValueError("incomplete download: got 5 of 12 bytes")  # ...then the stream dies

    monkeypatch.setattr(fwv, "_safe_streamed_download", _truncated)
    p2 = vault.download_firmware("bruce")
    assert p2 is None                                   # the re-download failed
    assert p1.read_bytes() == b"GOODFIRMWARE"           # existing good copy UNTOUCHED
    assert not p1.with_name(p1.name + ".part").exists()  # partial temp discarded
    assert vault.get_cached("bruce") == p1              # index still resolves to the good file


def test_sha_mismatch_discards_temp_and_stores_nothing(vault, monkeypatch):
    _wire_merged_profile(vault, monkeypatch, sha_pin="00" * 32)  # a pin the body won't match
    monkeypatch.setattr(fwv, "_safe_streamed_download", _good_download(b"WRONGBODY"))

    p = vault.download_firmware("bruce")
    assert p is None                                    # hard-fail on pin mismatch
    assert vault.get_cached("bruce") is None            # nothing indexed
    assert list((vault.vault_dir / "bruce" / "v1").glob("*")) == []  # no .bin AND no .part left
