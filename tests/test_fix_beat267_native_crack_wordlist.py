r"""Beat 267 - native_crack wordlist splitting + PMK reuse (cc-deep-audit-11 crack cluster).

Two confirmed findings in src/core/native_crack.py::crack, both 3/3:

  [0] MED - the wordlist was opened "rb" and iterated `for raw in f`, splitting on b"\n" ONLY.
      A classic-Mac (lone-CR) or CR-separated wordlist is yielded as ONE blob, so the real per-line
      passphrases are never derived -> crack reports the standard "key not in wordlist" even
      though the key is in the file (a verify-never-fake violation), and a large newline-free file
      is slurped whole into RAM. Fix: _iter_wordlist_lines reads bounded chunks, normalizes b"\r" to
      b"\n", and caps a delimiter-free run at _MAX_LINE_BYTES.

  [5] LOW - the inner loop called verify(hs, psk) per handshake, and verify recomputes the
      4096-round PBKDF2 PMK internally. The PMK depends only on (psk, essid), so N same-ESSID
      handshakes recomputed it N times per candidate. Fix: derive the PMK once per distinct salt
      per candidate (_verify_with_pmk takes an already-derived PMK).

Discriminating (both fail on buggy HEAD, pass on the fix):
  - test_lone_cr_wordlist_finds_middle_key: a real PMKID handshake whose key is the MIDDLE entry
    of a lone-CR wordlist; the fix cracks it, HEAD reports "not in wordlist".
  - test_pmk_derived_once_per_shared_salt: two same-ESSID handshakes + 3 non-matching candidates;
    the fix calls pmk 3x (once per candidate), HEAD calls it 6x (once per handshake per candidate).
Guards (pass on both): a normal CRLF wordlist still cracks; two distinct ESSIDs still derive each.
"""
from __future__ import annotations

import pytest

from src.core import native_crack as nc

_AP1 = bytes.fromhex("aabbccddeef1")
_STA1 = bytes.fromhex("111111111111")
_AP2 = bytes.fromhex("aabbccddeef2")
_STA2 = bytes.fromhex("222222222222")


def _pmkid_hs(essid: str, ap: bytes, sta: bytes, psk: bytes) -> "nc.Handshake":
    """A genuinely crackable PMKID handshake for *psk* (built with the module's own crypto)."""
    the_pmk = nc.pmk(psk, essid)
    return nc.Handshake(kind="pmkid", essid=essid, ap_mac=ap, sta_mac=sta,
                        essid_bytes=essid.encode("utf-8"),
                        pmkid=nc.compute_pmkid(the_pmk, ap, sta))


def test_lone_cr_wordlist_finds_middle_key(tmp_path):
    """[0] discriminator: the key is the MIDDLE lone-CR entry; HEAD's blob can't isolate it."""
    key = b"correcthorse"
    hs = _pmkid_hs("TestNet-CR", _AP1, _STA1, key)
    wl = tmp_path / "mac.txt"
    # classic-Mac line endings (b"\r"), key is NOT the last entry
    wl.write_bytes(b"aaaaaaaa\r" + key + b"\rzzzzzzzz\r")

    res = nc.crack([hs], str(wl))

    assert res.cracked is True, "the lone-CR wordlist's real key must be tested and recovered"
    assert res.password == "correcthorse"
    assert res.tried >= 2, "every CR-separated candidate must be tried, not one concatenated blob"


def test_crlf_wordlist_still_cracks(tmp_path):
    """No-regression guard (passes on HEAD + fix): a normal CRLF wordlist still finds the key."""
    key = b"correcthorse"
    hs = _pmkid_hs("TestNet-CRLF", _AP1, _STA1, key)
    wl = tmp_path / "crlf.txt"
    wl.write_bytes(b"aaaaaaaa\r\n" + key + b"\r\nzzzzzzzz\r\n")

    res = nc.crack([hs], str(wl))

    assert res.cracked is True
    assert res.password == "correcthorse"


def _counting_pmk(monkeypatch):
    calls = {"n": 0}
    real = nc.pmk

    def counting(psk, essid):
        calls["n"] += 1
        return real(psk, essid)

    monkeypatch.setattr(nc, "pmk", counting)
    return calls


def _nonmatching_wordlist(tmp_path) -> str:
    wl = tmp_path / "wl.txt"
    wl.write_bytes(b"aaaaaaaa\nbbbbbbbb\ncccccccc\n")  # 3 valid-length candidates, none matches
    return str(wl)


def test_pmk_derived_once_per_shared_salt(tmp_path, monkeypatch):
    """[5] discriminator: two same-ESSID handshakes derive the PMK ONCE per candidate, not twice."""
    # pmkid built from a psk that is NOT in the wordlist -> full scan, no early return
    absent = nc.pmk(b"not-in-list-xyz", "SharedNet")
    hs1 = nc.Handshake(kind="pmkid", essid="SharedNet", ap_mac=_AP1, sta_mac=_STA1,
                       essid_bytes=b"SharedNet", pmkid=nc.compute_pmkid(absent, _AP1, _STA1))
    hs2 = nc.Handshake(kind="pmkid", essid="SharedNet", ap_mac=_AP2, sta_mac=_STA2,
                       essid_bytes=b"SharedNet", pmkid=nc.compute_pmkid(absent, _AP2, _STA2))
    calls = _counting_pmk(monkeypatch)

    res = nc.crack([hs1, hs2], _nonmatching_wordlist(tmp_path))

    assert res.cracked is False
    assert calls["n"] == 3, "one PMK per candidate for a shared salt (HEAD recomputes per hs -> 6)"


def test_distinct_essids_derive_each_salt(tmp_path, monkeypatch):
    """Guard (passes on HEAD + fix): two DIFFERENT ESSIDs still derive a PMK for each salt."""
    a1 = nc.pmk(b"not-in-list-xyz", "NetA")
    a2 = nc.pmk(b"not-in-list-xyz", "NetB")
    hs1 = nc.Handshake(kind="pmkid", essid="NetA", ap_mac=_AP1, sta_mac=_STA1,
                       essid_bytes=b"NetA", pmkid=nc.compute_pmkid(a1, _AP1, _STA1))
    hs2 = nc.Handshake(kind="pmkid", essid="NetB", ap_mac=_AP2, sta_mac=_STA2,
                       essid_bytes=b"NetB", pmkid=nc.compute_pmkid(a2, _AP2, _STA2))
    calls = _counting_pmk(monkeypatch)

    res = nc.crack([hs1, hs2], _nonmatching_wordlist(tmp_path))

    assert res.cracked is False
    assert calls["n"] == 6, "two distinct salts x 3 candidates = 6 (no wrong cache collapse)"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
