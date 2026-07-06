"""Tests for the Software-OS flashing catalog (src/core/os_catalog.py).

Network + the destructive device write are mocked (monkeypatch), mirroring tests/test_tails.py.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from src.core import os_catalog as oc


def _silent(_):  # on_line sink
    pass


@pytest.fixture()
def img(tmp_path):
    p = tmp_path / "os-image.iso"
    p.write_bytes(b"OS-IMAGE-CONTENT" * 2000)
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    return str(p), sha


# ── catalog + allowlist ──────────────────────────────────────────────

def test_load_catalog_lists_exactly_the_known_oses():
    # Drift-lock: the OS flasher's catalog is exactly these four. Adding or removing an OS has
    # to be a conscious change here (and in the README / how-to OS list), not a silent drift.
    ids = {i.id for i in oc.load_catalog()}
    assert ids == {"tails", "kali", "arch", "parrot"}
    arch = oc.get_image("arch")
    assert arch.verify_model == "image_sig" and arch.image_type == "iso"
    kali = oc.get_image("kali")
    assert kali.verify_model == "checksums_sig" and kali.extra.get("kali_variant") == "live-amd64"


def test_load_catalog_includes_parrot():
    ids = {i.id for i in oc.load_catalog()}
    assert "parrot" in ids
    p = oc.get_image("parrot")
    assert p.resolver == "parrot"
    assert p.image_type == "iso"
    assert p.verify_model == "image_sig"
    assert p.gpg_fingerprint == "B711822346552E4D92DA02DF7A8286AF0E81EE4A"
    assert p.extra.get("parrot_edition") == "security"  # unknown JSON key -> .extra
    assert p.pinned.get("sha256") == \
        "fe8ec64f92d8d629b1fcae85d9fab81c87e3ff30584201e82b7c453a740cefbc"


def test_host_allowlist():
    assert oc._host_allowed("cdimage.kali.org") is True
    assert oc._host_allowed("geo.mirror.pkgbuild.com") is True
    assert oc._host_allowed("download.tails.net") is True
    assert oc._host_allowed("evil.example.com") is False
    with pytest.raises(ValueError):
        oc._require_os_url("https://evil.example.com/x.iso")
    with pytest.raises(ValueError):
        oc._require_os_url("http://cdimage.kali.org/x.iso")  # non-https


def test_parse_sha256sums():
    body = ("a" * 64 + "  kali-linux-2026.2-live-amd64.iso\n"
            + "b" * 64 + " *kali-linux-2026.2-installer-amd64.iso\n")
    assert oc.parse_sha256sums(body, "kali-linux-2026.2-live-amd64.iso") == "a" * 64
    assert oc.parse_sha256sums(body, "kali-linux-2026.2-installer-amd64.iso") == "b" * 64
    assert oc.parse_sha256sums(body, "nope.iso") is None


# ── resolvers ────────────────────────────────────────────────────────

def test_resolve_kali(monkeypatch):
    body = ("c" * 64 + "  kali-linux-2026.2-live-amd64.iso\n"
            + "d" * 64 + "  kali-linux-2026.2-installer-amd64.iso\n")
    monkeypatch.setattr(oc, "_http_get_text", lambda url, timeout=30: body)
    r = oc.resolve(oc.get_image("kali"), _silent, online=True)
    assert r.source == "online" and r.version == "2026.2"
    assert r.image_url == "https://cdimage.kali.org/current/kali-linux-2026.2-live-amd64.iso"
    assert r.sha256 == "c" * 64 and r.verify_model == "checksums_sig"
    assert r.checksums_sig_url.endswith("SHA256SUMS.gpg")


def test_resolve_arch(monkeypatch):
    feed = {"latest_version": "2026.06.01", "releases": [
        {"version": "2026.05.01", "available": True, "iso_url": "/iso/2026.05.01/archlinux-2026.05.01-x86_64.iso",
         "sha256_sum": "e" * 64, "release_date": "2026-05-01"},
        {"version": "2026.06.01", "available": True, "iso_url": "/iso/2026.06.01/archlinux-2026.06.01-x86_64.iso",
         "sha256_sum": "f" * 64, "pgp_fingerprint": "ABCD1234", "release_date": "2026-06-01"},
    ]}
    monkeypatch.setattr(oc, "_http_get_json", lambda url, timeout=30: feed)
    r = oc.resolve(oc.get_image("arch"), _silent, online=True)
    assert r.version == "2026.06.01" and r.sha256 == "f" * 64
    assert r.image_url == "https://geo.mirror.pkgbuild.com/iso/2026.06.01/archlinux-2026.06.01-x86_64.iso"
    assert r.sig_url == r.image_url + ".sig"
    assert r.gpg_fingerprint == "ABCD1234"  # read per-release from the feed


def test_resolve_parrot(monkeypatch):
    index = ('<a href="6.4/">6.4/</a>\n<a href="7.2/">7.2/</a>\n'
             '<a href="7.3/">7.3/</a>\n<a href="latest/">latest/</a>\n')
    sums = ("md5\n" + "0" * 32 + "  Parrot-security-7.3_amd64.iso\n\n"
            "sha256\n" + "a" * 64 + "  Parrot-security-7.3_amd64.iso\n"
            + "b" * 64 + "  Parrot-home-7.3_amd64.iso\n\n"
            "sha512\n" + "c" * 128 + "  Parrot-security-7.3_amd64.iso\n")

    def fake_get_text(url, timeout=30):
        return sums if url.endswith("signed-hashes.txt") else index

    monkeypatch.setattr(oc, "_http_get_text", fake_get_text)
    r = oc.resolve(oc.get_image("parrot"), _silent, online=True)
    assert r.source == "online" and r.version == "7.3"  # highest semver dir, not "latest"
    assert r.image_url == "https://deb.parrot.sh/parrot/iso/7.3/Parrot-security-7.3_amd64.iso"
    assert r.sha256 == "a" * 64 and r.verify_model == "image_sig"
    assert r.sig_url is None  # no detached per-ISO sig for Parrot
    assert r.gpg_fingerprint == "B711822346552E4D92DA02DF7A8286AF0E81EE4A"


def test_resolve_parrot_offline_uses_pinned():
    r = oc.resolve(oc.get_image("parrot"), _silent, online=False)
    assert r.source == "pinned" and r.version == "7.3"
    assert r.verify_model == "image_sig"
    assert r.sha256 == "fe8ec64f92d8d629b1fcae85d9fab81c87e3ff30584201e82b7c453a740cefbc"


def test_resolve_tails(monkeypatch):
    monkeypatch.setattr(oc._tails, "try_fetch_latest", lambda on: {
        "version": "7.9", "sha256": "1" * 64,
        "url": "https://download.tails.net/tails/stable/tails-amd64-7.9/tails-amd64-7.9.img"})
    r = oc.resolve(oc.get_image("tails"), _silent, online=True)
    assert r.version == "7.9" and r.verify_model == "image_sig"
    assert r.sig_url == "https://tails.net/torrents/files/tails-amd64-7.9.img.sig"
    assert r.sha256 == "1" * 64


def test_resolve_offline_uses_pinned():
    r = oc.resolve(oc.get_image("arch"), _silent, online=False)
    assert r.source == "pinned" and r.version == "2026.06.01"
    assert r.sha256 == "ec7a9c89aed7a59a76266ccf723c5e88480e47d7088c4482436f882fa37c3989"


def test_resolve_falls_back_on_network_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(oc, "_http_get_json", boom)
    r = oc.resolve(oc.get_image("arch"), _silent, online=True)
    assert r.source == "pinned"  # resolver raised -> pinned fallback


# ── flash control flow ───────────────────────────────────────────────

def _resolved_image_sig(image_id, sha):
    return oc.Resolved(image_id=image_id, version="x", image_url="https://x", image_type="iso",
                       verify_model="image_sig", sha256=sha)


def test_flash_requires_confirmation(img):
    path, sha = img
    with pytest.raises(ValueError, match="confirmed=True"):
        oc.flash_os_image(oc.get_image("arch"), _resolved_image_sig("arch", sha), path,
                          r"\\.\PhysicalDrive9", _silent, confirmed=False)


def test_flash_image_sig_success_with_sha(img, monkeypatch):
    path, sha = img
    monkeypatch.setattr(oc.sd, "write_image", lambda *a, **k: 0)
    monkeypatch.setattr(oc.sd, "verify_write", lambda *a, **k: True)
    rc = oc.flash_os_image(oc.get_image("arch"), _resolved_image_sig("arch", sha), path,
                           r"\\.\PhysicalDrive9", _silent, confirmed=True)
    assert rc == 0


def test_flash_rejects_sha_mismatch(img, monkeypatch):
    path, _sha = img
    wrote = {"x": False}
    monkeypatch.setattr(oc.sd, "write_image", lambda *a, **k: wrote.__setitem__("x", True) or 0)
    with pytest.raises(ValueError, match="SHA-256"):
        oc.flash_os_image(oc.get_image("arch"), _resolved_image_sig("arch", "0" * 64), path,
                          r"\\.\PhysicalDrive9", _silent, confirmed=True)
    assert wrote["x"] is False


def test_flash_bad_signature_refuses(img, monkeypatch):
    path, sha = img
    monkeypatch.setattr(oc, "verify_gpg_detached", lambda *a, **k: False)
    monkeypatch.setattr(oc.sd, "write_image", lambda *a, **k: 0)
    r = _resolved_image_sig("arch", sha)
    with pytest.raises(ValueError, match="signature is NOT valid"):
        oc.flash_os_image(oc.get_image("arch"), r, path, r"\\.\PhysicalDrive9", _silent,
                          sig_path=path + ".sig", confirmed=True)


def test_flash_checksums_sig_kali_success(img, monkeypatch):
    path, sha = img
    monkeypatch.setattr(oc.sd, "write_image", lambda *a, **k: 0)
    monkeypatch.setattr(oc.sd, "verify_write", lambda *a, **k: True)
    monkeypatch.setattr(oc, "verify_gpg_detached", lambda *a, **k: True)
    r = oc.Resolved(image_id="kali", version="2026.2", image_url="https://x", image_type="iso",
                    verify_model="checksums_sig", sha256=sha)
    rc = oc.flash_os_image(oc.get_image("kali"), r, path, r"\\.\PhysicalDrive9", _silent,
                           checksums_path=path, checksums_sig_path=path + ".gpg", confirmed=True)
    assert rc == 0


def test_flash_checksums_sig_bad_sig_refuses(img, monkeypatch):
    path, sha = img
    monkeypatch.setattr(oc, "verify_gpg_detached", lambda *a, **k: False)
    monkeypatch.setattr(oc.sd, "write_image", lambda *a, **k: 0)
    r = oc.Resolved(image_id="kali", version="2026.2", image_url="https://x", image_type="iso",
                    verify_model="checksums_sig", sha256=sha)
    with pytest.raises(ValueError, match="SHA256SUMS signature"):
        oc.flash_os_image(oc.get_image("kali"), r, path, r"\\.\PhysicalDrive9", _silent,
                          checksums_path=path, checksums_sig_path=path + ".gpg", confirmed=True)


# ── OS-integrity hardening (deep review round 2): unpinned-key rubber-stamp + Parrot clearsign ──

_PARROT_FPR = "B711822346552E4D92DA02DF7A8286AF0E81EE4A"


def _mock_gpg_status(monkeypatch, status):
    monkeypatch.setattr(oc, "_gpg", lambda: "gpg")

    class _P:
        def __init__(self):
            self.stdout, self.stderr = status, ""

    monkeypatch.setattr(oc.subprocess, "run", lambda *a, **k: _P())


def test_verify_gpg_detached_no_fingerprint_defers_not_trusts(monkeypatch):
    # A perfectly good signature but NO pinned fingerprint must return None (defer to SHA), never True —
    # otherwise ANY key in the keyring (incl. an attacker's) would pass as "trusted".
    _mock_gpg_status(monkeypatch, "[GNUPG:] VALIDSIG DEADBEEF\n[GNUPG:] GOODSIG\n")
    assert oc.verify_gpg_detached("img", "sig", None, _silent) is None


def test_verify_clearsigned_good_matching_fingerprint(monkeypatch):
    _mock_gpg_status(monkeypatch, f"[GNUPG:] VALIDSIG {_PARROT_FPR}\n[GNUPG:] GOODSIG\n")
    assert oc.verify_gpg_clearsigned("hashes.txt", _PARROT_FPR, _silent) is True


def test_verify_clearsigned_wrong_fingerprint_is_false(monkeypatch):
    _mock_gpg_status(monkeypatch, "[GNUPG:] VALIDSIG " + "0" * 40 + "\n[GNUPG:] GOODSIG\n")
    assert oc.verify_gpg_clearsigned("hashes.txt", _PARROT_FPR, _silent) is False


def test_verify_clearsigned_no_fingerprint_or_no_gpg_is_none(monkeypatch):
    _mock_gpg_status(monkeypatch, f"[GNUPG:] VALIDSIG {_PARROT_FPR}\n")
    assert oc.verify_gpg_clearsigned("hashes.txt", None, _silent) is None  # no pinned key
    monkeypatch.setattr(oc, "_gpg", lambda: None)
    assert oc.verify_gpg_clearsigned("hashes.txt", _PARROT_FPR, _silent) is None  # no gpg


def _resolved_parrot(sha):
    return oc.Resolved(image_id="parrot", version="7.3", image_url="https://x", image_type="iso",
                       verify_model="image_sig", checksums_url="https://x/signed-hashes.txt",
                       sha256=sha, gpg_fingerprint=_PARROT_FPR)


def test_flash_parrot_good_clearsign_writes(img, monkeypatch):
    path, sha = img
    monkeypatch.setattr(oc, "verify_gpg_clearsigned", lambda *a, **k: True)
    monkeypatch.setattr(oc.sd, "write_image", lambda *a, **k: 0)
    monkeypatch.setattr(oc.sd, "verify_write", lambda *a, **k: True)
    rc = oc.flash_os_image(oc.get_image("parrot"), _resolved_parrot(sha), path,
                           r"\\.\PhysicalDrive9", _silent, checksums_path=path, confirmed=True)
    assert rc == 0


def test_flash_parrot_bad_clearsign_refuses_without_writing(img, monkeypatch):
    path, sha = img
    wrote = {"x": False}
    monkeypatch.setattr(oc, "verify_gpg_clearsigned", lambda *a, **k: False)
    monkeypatch.setattr(oc.sd, "write_image", lambda *a, **k: wrote.__setitem__("x", True) or 0)
    with pytest.raises(ValueError, match="Clearsigned hashes are NOT valid"):
        oc.flash_os_image(oc.get_image("parrot"), _resolved_parrot(sha), path,
                          r"\\.\PhysicalDrive9", _silent, checksums_path=path, confirmed=True)
    assert wrote["x"] is False


def test_flash_parrot_no_gpg_falls_to_sha_with_note(img, monkeypatch):
    path, sha = img
    monkeypatch.setattr(oc, "verify_gpg_clearsigned", lambda *a, **k: None)  # gpg unavailable
    monkeypatch.setattr(oc.sd, "write_image", lambda *a, **k: 0)
    monkeypatch.setattr(oc.sd, "verify_write", lambda *a, **k: True)
    msgs: list[str] = []
    rc = oc.flash_os_image(oc.get_image("parrot"), _resolved_parrot(sha), path,
                           r"\\.\PhysicalDrive9", msgs.append, checksums_path=path, confirmed=True)
    assert rc == 0
    assert any("was not verified" in m for m in msgs), msgs  # honest: not full PGP assurance
