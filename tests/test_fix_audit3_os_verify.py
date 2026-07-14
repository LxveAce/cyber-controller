"""Regression guard for cc-deep-audit-3 O1 (2026-07-13): os_catalog silent-verification downgrade.

`flash_os_image`'s image_sig path (Tails/Arch) sets `verified = True` off a bare SHA-256 match when
the GPG signature could NOT be checked (gpg missing, or the detached .sig absent). The disclosure
NOTE was gated on a `checksums_path`, but the detached-sig profiles carry NO hashes file — so a
match against a NETWORK-fetched sha (Tails' hash rides the same fetch as the image) authenticated
the write with **no note and no warning**. A MITM serving a matched image+hash pair passed unseen.
The fix makes the disclosure UNCONDITIONAL: whenever the write falls back to a bare SHA match
without a verified signature, the operator is told it's integrity-only, not authenticated.

Network + the destructive device write are mocked, mirroring tests/test_os_catalog.py.
"""
from __future__ import annotations

import hashlib

import pytest

from src.core import os_catalog as oc

_DRIVE = r"\\.\PhysicalDrive9"


@pytest.fixture()
def img(tmp_path):
    p = tmp_path / "os-image.iso"
    p.write_bytes(b"OS-IMAGE-CONTENT" * 2000)
    return str(p), hashlib.sha256(p.read_bytes()).hexdigest()


def _resolved_image_sig(image_id, sha):
    return oc.Resolved(image_id=image_id, version="x", image_url="https://x", image_type="iso",
                       verify_model="image_sig", sha256=sha)


def _no_write(monkeypatch):
    monkeypatch.setattr(oc.sd, "write_image", lambda *a, **k: 0)
    monkeypatch.setattr(oc.sd, "verify_write", lambda *a, **k: True)


def test_bare_sha_without_verified_sig_is_disclosed_not_silent(img, monkeypatch):
    """image_sig + a SHA that matches but a GPG sig that can't be checked (no hashes file) must
    DISCLOSE the write is integrity-only — previously silent for the detached-sig profiles."""
    path, sha = img
    _no_write(monkeypatch)
    monkeypatch.setattr(oc, "verify_gpg_detached", lambda *a, **k: None)  # gpg cannot verify
    lines: list = []
    r = _resolved_image_sig("arch", sha)  # no checksums_path -> the old code emitted NOTHING here
    rc = oc.flash_os_image(oc.get_image("arch"), r, path, _DRIVE, lines.append,
                           sig_path=path + ".sig", confirmed=True)
    assert rc == 0
    assert any("integrity check only" in ln.lower() for ln in lines), lines
    # the write DID have a matching checksum, so the blanket "no valid signature/checksum" line
    # must NOT appear (that would be inaccurate) — the precise NOTE is the honest disclosure.
    assert not any("no valid signature/checksum" in ln.lower() for ln in lines), lines


def test_bare_sha_no_sig_file_at_all_still_discloses(img, monkeypatch):
    """Even with no signature file passed (sig verification never runs), a bare SHA match must still
    be disclosed as integrity-only rather than treated as a clean, silent success."""
    path, sha = img
    _no_write(monkeypatch)
    lines: list = []
    rc = oc.flash_os_image(oc.get_image("arch"), _resolved_image_sig("arch", sha), path, _DRIVE,
                           lines.append, confirmed=True)
    assert rc == 0
    assert any("integrity check only" in ln.lower() for ln in lines), lines


def test_verified_signature_emits_no_integrity_disclaimer(img, monkeypatch):
    """When the detached signature ACTUALLY verifies, the write is truly authenticated — the
    'integrity check only' disclaimer must NOT appear (it would falsely undersell a real verify)."""
    path, sha = img
    _no_write(monkeypatch)
    monkeypatch.setattr(oc, "verify_gpg_detached", lambda *a, **k: True)
    lines: list = []
    r = _resolved_image_sig("arch", sha)
    rc = oc.flash_os_image(oc.get_image("arch"), r, path, _DRIVE, lines.append,
                           sig_path=path + ".sig", confirmed=True)
    assert rc == 0
    assert not any("integrity check only" in ln.lower() for ln in lines), lines
