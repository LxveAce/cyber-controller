"""Tests for Tails flashing (src/core/tails.py). The destructive device write is mocked."""

from __future__ import annotations

import hashlib

import pytest

from src.core import tails


@pytest.fixture()
def img(tmp_path):
    p = tmp_path / "tails-amd64-6.0.img"
    p.write_bytes(b"TAILS-IMAGE-CONTENT" * 1000)
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    return p, sha


def _silent(_):  # on_line sink
    pass


def test_verify_sha256(img):
    p, sha = img
    assert tails.verify_sha256(str(p), sha, _silent) is True
    assert tails.verify_sha256(str(p), "0" * 64, _silent) is False
    assert tails.verify_sha256(str(p), "not-a-hash", _silent) is False
    # case/space-insensitive
    assert tails.verify_sha256(str(p), sha.upper(), _silent) is True


def test_flash_requires_confirmation(img):
    p, sha = img
    with pytest.raises(ValueError, match="confirmed=True"):
        tails.flash_local_image(str(p), r"\\.\PhysicalDrive9", _silent, expected_sha256=sha, confirmed=False)


def test_flash_rejects_iso(tmp_path):
    iso = tmp_path / "tails.iso"
    iso.write_bytes(b"x" * 100)
    with pytest.raises(ValueError, match="WRONG file|\\.img"):
        tails.flash_local_image(str(iso), r"\\.\PhysicalDrive9", _silent, confirmed=True)


def test_flash_rejects_sha_mismatch(img, monkeypatch):
    p, _sha = img
    called = {"write": False}
    monkeypatch.setattr(tails.sd, "write_image", lambda *a, **k: called.__setitem__("write", True) or 0)
    with pytest.raises(ValueError, match="SHA-256"):
        tails.flash_local_image(str(p), r"\\.\PhysicalDrive9", _silent, expected_sha256="0" * 64, confirmed=True)
    assert called["write"] is False  # never wrote on mismatch


def test_flash_success_with_sha(img, monkeypatch):
    p, sha = img
    monkeypatch.setattr(tails.sd, "write_image", lambda *a, **k: 0)
    monkeypatch.setattr(tails.sd, "verify_write", lambda *a, **k: True)
    rc = tails.flash_local_image(str(p), r"\\.\PhysicalDrive9", _silent, expected_sha256=sha, confirmed=True)
    assert rc == 0


def test_flash_unverified_warns_but_writes(img, monkeypatch):
    p, _sha = img
    lines = []
    monkeypatch.setattr(tails.sd, "write_image", lambda *a, **k: 0)
    monkeypatch.setattr(tails.sd, "verify_write", lambda *a, **k: True)
    rc = tails.flash_local_image(str(p), r"\\.\PhysicalDrive9", lines.append, confirmed=True)
    assert rc == 0
    assert any("UNVERIFIED" in ln for ln in lines)


def test_flash_write_failure_propagates(img, monkeypatch):
    p, sha = img
    monkeypatch.setattr(tails.sd, "write_image", lambda *a, **k: 5)
    rc = tails.flash_local_image(str(p), r"\\.\PhysicalDrive9", _silent, expected_sha256=sha, confirmed=True)
    assert rc == 5


def test_missing_image(tmp_path):
    with pytest.raises(FileNotFoundError):
        tails.flash_local_image(str(tmp_path / "nope.img"), r"\\.\PhysicalDrive9", _silent, confirmed=True)


def test_host_allowlist():
    assert tails._host_allowed("download.tails.net") is True
    assert tails._host_allowed("tails.net") is True
    assert tails._host_allowed("evil.example.com") is False
    with pytest.raises(ValueError):
        tails._require_tails_url("https://evil.example.com/tails.img")
    with pytest.raises(ValueError):
        tails._require_tails_url("http://download.tails.net/x.img")  # non-https


# ── download_image: streamed socket must be closed deterministically ──
#
# Regression for a leaked streamed connection: download_image() opens each hop with stream=True,
# so the socket stays live until resp.close(). If the close only ran *after* the redirect-allowlist
# check (or after raise_for_status), an off-allowlist redirect or an HTTP error would raise before
# the close, leaking the socket to GC finalization. The fix wraps the loop body in
# try/finally: resp.close() (mirroring firmware_vault._safe_streamed_download).

class _FakeResp:
    def __init__(self, *, is_redirect=False, location="", raise_http=False):
        self.is_redirect = is_redirect
        self.is_permanent_redirect = False
        self.headers = {"Location": location} if location else {}
        self._raise_http = raise_http
        self.closed = False

    def raise_for_status(self):
        if self._raise_http:
            raise tails.requests.HTTPError("500 Server Error")

    def iter_content(self, chunk_size=1):
        return iter([b"data"])

    def close(self):
        self.closed = True


def test_download_image_closes_response_on_offallowlist_redirect(tmp_path, monkeypatch):
    resp = _FakeResp(is_redirect=True, location="https://evil.example.com/pwn.img")
    monkeypatch.setattr(tails.requests, "get", lambda *a, **k: resp)
    with pytest.raises(ValueError):
        tails.download_image("https://download.tails.net/tails.img", str(tmp_path), _silent)
    assert resp.closed is True  # socket released before the allowlist ValueError propagated


def test_download_image_closes_response_on_http_error(tmp_path, monkeypatch):
    resp = _FakeResp(raise_http=True)
    monkeypatch.setattr(tails.requests, "get", lambda *a, **k: resp)
    with pytest.raises(tails.requests.HTTPError):
        tails.download_image("https://download.tails.net/tails.img", str(tmp_path), _silent)
    assert resp.closed is True  # streamed body left unconsumed, but the socket was still closed
