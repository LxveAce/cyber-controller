"""Beat 262 — os_catalog verify-launder (cc-deep-audit-10 [0] HIGH Kali + [1] HIGH Parrot).

`flash_os_image` verified the GPG signature over the *download-time* `checksums_path`, but
enforced the SHA-256 in `resolved.sha256` — a value parsed during a SEPARATE, unsigned
*resolve-time* GET of the same hashes file (`_resolve_kali` os_catalog.py:204, `_resolve_parrot`
:268). Those are two independent HTTP fetches, so a malicious mirror / TLS-MITM (exactly the
threat the PGP signature exists to counter) can serve a **forged, unsigned** hash list at resolve
time (listing the hash of a swapped image) and the **genuine, signed** list at download time. The
signature then validates one byte-stream while a different, unauthenticated byte-stream supplies
the trusted hash — and for Kali the "not fully verified" note is even suppressed, so the operator
who imported the pinned key is told the image is signed.

Fix: when the signature/clearsign verifies, re-parse the enforced hash FROM the signature-verified
`checksums_path` (never `resolved.sha256`); fall back to `resolved.sha256` only as a disclosed,
unauthenticated integrity anchor when no signature could be established.

Discriminating: the two launder tests pass on the fix (the write is REFUSED) and fail on HEAD (the
write succeeds). The happy-path + no-signature guards pass on both. Network and the destructive
device write are mocked, mirroring tests/test_os_catalog.py and tests/test_fix_audit3_os_verify.py.
"""
from __future__ import annotations

import hashlib

import pytest

from src.core import os_catalog as oc

_DRIVE = r"\\.\PhysicalDrive9"
_GENUINE = "b" * 64  # a valid 64-hex that is NOT the on-disk image's real hash


def _no_write(monkeypatch):
    monkeypatch.setattr(oc.sd, "write_image", lambda *a, **k: 0)
    monkeypatch.setattr(oc.sd, "verify_write", lambda *a, **k: True)


def _image(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"MALICIOUS-OR-REAL-IMAGE" * 1000)
    return str(p), hashlib.sha256(p.read_bytes()).hexdigest()


def _sums_file(tmp_path, listed_sha, image_name):
    f = tmp_path / "SHA256SUMS"
    f.write_text(f"{listed_sha}  {image_name}\n", encoding="utf-8")
    return str(f)


# ── Kali (checksums_sig / detached GPG over SHA256SUMS) ───────────────

def test_kali_enforces_signed_hash_not_resolve_time_hash(tmp_path, monkeypatch):
    """LAUNDER: sig verifies over checksums_path, but resolved.sha256 (unsigned, resolve-time)
    lists the malicious image's hash; the signed file lists the genuine -> fix must REFUSE."""
    _no_write(monkeypatch)
    monkeypatch.setattr(oc, "verify_gpg_detached", lambda *a, **k: True)  # signed file checks out
    name = "kali-linux-2025.1-live-amd64.iso"
    path, evil_sha = _image(tmp_path, name)
    sums = _sums_file(tmp_path, _GENUINE, name)  # signed list = the genuine (different) hash
    r = oc.Resolved(image_id="kali", version="x", image_url="https://x", image_type="iso",
                    verify_model="checksums_sig", checksums_url="https://x", sha256=evil_sha)
    # Buggy code enforced resolved.sha256 (== the on-disk malicious hash) and wrote the image.
    with pytest.raises(ValueError):
        oc.flash_os_image(oc.get_image("kali"), r, path, _DRIVE, lambda s: None,
                          checksums_path=sums, checksums_sig_path=path + ".sig", confirmed=True)


def test_kali_signed_hash_match_still_flashes(tmp_path, monkeypatch):
    """No-regression: a genuine signed Kali download (signed list carries the real image hash)
    still flashes. Passes on HEAD and on the fix."""
    _no_write(monkeypatch)
    monkeypatch.setattr(oc, "verify_gpg_detached", lambda *a, **k: True)
    name = "kali-linux-2025.1-live-amd64.iso"
    path, real_sha = _image(tmp_path, name)
    sums = _sums_file(tmp_path, real_sha, name)
    r = oc.Resolved(image_id="kali", version="x", image_url="https://x", image_type="iso",
                    verify_model="checksums_sig", checksums_url="https://x", sha256=real_sha)
    rc = oc.flash_os_image(oc.get_image("kali"), r, path, _DRIVE, lambda s: None,
                           checksums_path=sums, checksums_sig_path=path + ".sig", confirmed=True)
    assert rc == 0


def test_kali_no_gpg_falls_back_to_disclosed_integrity_anchor(tmp_path, monkeypatch):
    """No-regression: when the signature can't be established (gpg missing / key not imported), the
    write still proceeds off the SHA anchor but is DISCLOSED as integrity-only. Guards fallback."""
    _no_write(monkeypatch)
    monkeypatch.setattr(oc, "verify_gpg_detached", lambda *a, **k: None)  # can't establish
    name = "kali-linux-2025.1-live-amd64.iso"
    path, real_sha = _image(tmp_path, name)
    sums = _sums_file(tmp_path, real_sha, name)
    lines: list = []
    r = oc.Resolved(image_id="kali", version="x", image_url="https://x", image_type="iso",
                    verify_model="checksums_sig", checksums_url="https://x", sha256=real_sha)
    rc = oc.flash_os_image(oc.get_image("kali"), r, path, _DRIVE, lines.append,
                           checksums_path=sums, checksums_sig_path=path + ".sig", confirmed=True)
    assert rc == 0
    assert any("signature was not verified" in ln.lower() for ln in lines), lines


# ── Parrot (image_sig with an inline-clearsigned hashes file) ─────────

def test_parrot_enforces_clearsigned_hash_not_resolve_time_hash(tmp_path, monkeypatch):
    """LAUNDER: clearsign verifies over checksums_path, but resolved.sha256 (unsigned, resolve-time)
    lists the malicious image's hash; the signed file lists the genuine -> fix must REFUSE."""
    _no_write(monkeypatch)
    monkeypatch.setattr(oc, "verify_gpg_clearsigned", lambda *a, **k: True)
    name = "Parrot-security-6.3_amd64.iso"
    path, evil_sha = _image(tmp_path, name)
    sums = _sums_file(tmp_path, _GENUINE, name)
    r = oc.Resolved(image_id="parrot", version="x", image_url="https://x", image_type="iso",
                    verify_model="image_sig", checksums_url="https://x", sha256=evil_sha)
    with pytest.raises(ValueError):
        oc.flash_os_image(oc.get_image("parrot"), r, path, _DRIVE, lambda s: None,
                          checksums_path=sums, confirmed=True)


def test_parrot_clearsigned_hash_match_still_flashes(tmp_path, monkeypatch):
    """No-regression: a genuine Parrot download (clearsigned list carries the real image hash)
    still flashes. Passes on HEAD and on the fix."""
    _no_write(monkeypatch)
    monkeypatch.setattr(oc, "verify_gpg_clearsigned", lambda *a, **k: True)
    name = "Parrot-security-6.3_amd64.iso"
    path, real_sha = _image(tmp_path, name)
    sums = _sums_file(tmp_path, real_sha, name)
    r = oc.Resolved(image_id="parrot", version="x", image_url="https://x", image_type="iso",
                    verify_model="image_sig", checksums_url="https://x", sha256=real_sha)
    rc = oc.flash_os_image(oc.get_image("parrot"), r, path, _DRIVE, lambda s: None,
                           checksums_path=sums, confirmed=True)
    assert rc == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
