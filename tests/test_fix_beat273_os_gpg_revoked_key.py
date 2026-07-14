"""Beat 273 - os_catalog GPG accepts a REVOKED/EXPIRED-key signature (cc-deep-audit-12 S2, HIGH).

`verify_gpg_detached` and `verify_gpg_clearsigned` keyed "good" off
`("VALIDSIG" in status or "GOODSIG" in status)`. A REVOKED (REVKEYSIG) or EXPIRED (EXPKEYSIG) key
-- or an expired signature (EXPSIG) -- is still cryptographically valid, so gpg emits VALIDSIG
(carrying the fingerprint) for it with NO GOODSIG. So a signature from a key the signer had REVOKED
(revocation is how a compromised key is invalidated) passed as VALID and the caller SKIPPED the
SHA-256 anchor -> an attacker with a compromised-then-revoked OS signing key could get a malicious
image flashed as "VALID". This is the exact twin of the tails.verify_gpg fix (beat 244, 30c87aa);
os_catalog was NOT actually doing it right (despite the tails comment claiming so).

Fix: gate on GOODSIG only, and hard-refuse REVKEYSIG/EXPKEYSIG/EXPSIG, in BOTH functions.

Discriminating (fail on buggy HEAD, pass on the fix):
  - test_detached_refuses_revoked_key / _expired_key: REVKEYSIG|EXPKEYSIG + VALIDSIG(fpr) -> False
    (HEAD returns True on the VALIDSIG).
  - test_clearsigned_refuses_revoked_key: same for the clearsigned path.
Guards (pass on both HEAD and the fix):
  - test_detached_accepts_current_goodsig / test_clearsigned_..._goodsig: GOODSIG -> True.
  - test_detached_missing_key_defers_to_sha: NO_PUBKEY -> None (defer to SHA-256).
"""
from __future__ import annotations

_FPR = "1234567890ABCDEF1234567890ABCDEF12345678"


def _patch_gpg(monkeypatch, status_out: str):
    import src.core.os_catalog as oc

    monkeypatch.setattr(oc, "which", lambda cand: "/usr/bin/gpg")

    class _Proc:
        stdout = status_out
        stderr = ""

    monkeypatch.setattr(oc.subprocess, "run", lambda *a, **k: _Proc())
    return oc


def test_detached_refuses_revoked_key(monkeypatch):
    status = f"[GNUPG:] REVKEYSIG 2589C84F Signer\n[GNUPG:] VALIDSIG {_FPR} 2024-01-01\n"
    oc = _patch_gpg(monkeypatch, status)
    assert oc.verify_gpg_detached("x.img", "x.img.sig", _FPR, lambda _s: None) is False


def test_detached_refuses_expired_key(monkeypatch):
    status = f"[GNUPG:] EXPKEYSIG 2589C84F Signer\n[GNUPG:] VALIDSIG {_FPR} 2024-01-01\n"
    oc = _patch_gpg(monkeypatch, status)
    assert oc.verify_gpg_detached("x.img", "x.img.sig", _FPR, lambda _s: None) is False


def test_clearsigned_refuses_revoked_key(monkeypatch):
    status = f"[GNUPG:] REVKEYSIG 2589C84F Signer\n[GNUPG:] VALIDSIG {_FPR} 2024-01-01\n"
    oc = _patch_gpg(monkeypatch, status)
    assert oc.verify_gpg_clearsigned("hashes.txt", _FPR, lambda _s: None) is False


def test_detached_accepts_current_goodsig(monkeypatch):
    status = f"[GNUPG:] GOODSIG 2589C84F Signer\n[GNUPG:] VALIDSIG {_FPR} 2024-01-01\n"
    oc = _patch_gpg(monkeypatch, status)
    assert oc.verify_gpg_detached("x.img", "x.img.sig", _FPR, lambda _s: None) is True


def test_clearsigned_accepts_current_goodsig(monkeypatch):
    status = f"[GNUPG:] GOODSIG 2589C84F Signer\n[GNUPG:] VALIDSIG {_FPR} 2024-01-01\n"
    oc = _patch_gpg(monkeypatch, status)
    assert oc.verify_gpg_clearsigned("hashes.txt", _FPR, lambda _s: None) is True


def test_detached_missing_key_defers_to_sha(monkeypatch):
    status = "[GNUPG:] NO_PUBKEY 2589C84F\n[GNUPG:] ERRSIG 2589C84F 1 8 00 0\n"
    oc = _patch_gpg(monkeypatch, status)
    assert oc.verify_gpg_detached("x.img", "x.img.sig", _FPR, lambda _s: None) is None


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
