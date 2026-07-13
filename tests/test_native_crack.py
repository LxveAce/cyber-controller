"""Correctness tests for CC's native WPA/WPA2 dictionary cracker (src/core/native_crack.py).

Validated against KNOWN-GOOD vectors so the crypto can't silently drift:
* PMK  — the canonical IEEE 802.11i / wpa_supplicant PSK->PMK vectors.
* PMKID + EAPOL-MIC — hashcat's own mode-22000 example hashes (plaintext "hashcat!", the 8-char
  WPA-valid form of hashcat's example password). If our math is right we reproduce them exactly.
"""
from __future__ import annotations

from src.core import native_crack as nc


def test_pmk_matches_canonical_vectors():
    assert nc.pmk("password", "IEEE").hex() == \
        "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e"
    assert nc.pmk("ThisIsAPassword", "ThisIsASSID").hex() == \
        "0dc0d6eb90555ed6419756b9a15ec3e3209b63df707dd508d14581f8982721af"


def test_pmkid_vector_hashcat():
    hs = nc.Handshake(
        kind="pmkid", essid="hashcat-essid",
        ap_mac=bytes.fromhex("fc690c158264"), sta_mac=bytes.fromhex("f4747f87f9f4"),
        pmkid=bytes.fromhex("4d4fe7aac3a2cecab195321ceb99a7d0"))
    assert nc.verify(hs, "hashcat!")
    assert not nc.verify(hs, "wrongpass")


# The hashcat mode-22000 example EAPOL frame (MIC field zeroed), verbatim — one literal to avoid a
# split-transcription error changing the byte content.
_EAPOL_HEX = "0103007502010a0000000000000000000148ce2ccba9c1fda130ff2fbbfb4fd3b063d1a93920b0f7df54a5cbf787b16171000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001630140100000fac040100000fac040100000fac028000"  # noqa: E501


def _eapol_hs():
    eapol = bytes.fromhex(_EAPOL_HEX)
    return nc.Handshake(
        kind="eapol", essid=bytes.fromhex("54502d4c494e4b5f484153484341545f54455354").decode(),
        ap_mac=bytes.fromhex("6466b38ec3fc"), sta_mac=bytes.fromhex("225edc49b7aa"),
        anonce=bytes.fromhex("10e3be3b005a629e89de088d6a2fdc489db83ad4764f2d186b9cde15446e972e"),
        snonce=eapol[17:49], eapol=eapol,
        mic=bytes.fromhex("024022795224bffca545276c3762686f"), key_version=eapol[6] & 0x07)


def test_eapol_mic_vector_hashcat():
    hs = _eapol_hs()
    assert hs.key_version == 2  # WPA2 / HMAC-SHA1
    assert nc.compute_mic(nc.pmk("hashcat!", hs.essid), hs).hex() == \
        "024022795224bffca545276c3762686f"
    assert nc.verify(hs, "hashcat!")
    assert not nc.verify(hs, "wrongpass")


def test_crack_recovers_from_wordlist(tmp_path):
    hs = nc.Handshake(
        kind="pmkid", essid="hashcat-essid",
        ap_mac=bytes.fromhex("fc690c158264"), sta_mac=bytes.fromhex("f4747f87f9f4"),
        pmkid=bytes.fromhex("4d4fe7aac3a2cecab195321ceb99a7d0"))
    wl = tmp_path / "w.txt"
    wl.write_text("short\nnotit123\nhashcat!\nafterwards\n", encoding="utf-8")
    res = nc.crack([hs], str(wl))
    assert res.cracked
    assert res.password == "hashcat!"
    assert res.essid == "hashcat-essid"


def test_crack_honest_negative(tmp_path):
    hs = nc.Handshake(
        kind="pmkid", essid="hashcat-essid",
        ap_mac=bytes.fromhex("fc690c158264"), sta_mac=bytes.fromhex("f4747f87f9f4"),
        pmkid=bytes.fromhex("4d4fe7aac3a2cecab195321ceb99a7d0"))
    wl = tmp_path / "w.txt"
    wl.write_text("aaaaaaaa\nbbbbbbbb\ncccccccc\n", encoding="utf-8")
    res = nc.crack([hs], str(wl))
    assert not res.cracked
    assert res.tried == 3
    assert "not in wordlist" in res.detail


def test_crack_empty_handshakes():
    res = nc.crack([], "nonexistent")
    assert not res.cracked
    assert "nothing to crack" in res.detail


def test_native_declines_aes_cmac_handshake(tmp_path):
    # A key-descriptor v3 (AES-128-CMAC / 802.11w-PMF) EAPOL handshake must be DECLINED, not silently
    # mis-verified with HMAC-SHA1. verify() returns False, and crack() reports it unsupported instead
    # of the misleading "key not in wordlist" (verify-never-fake: never a false match, never a false
    # "exhausted" that hides an uncrackable-here handshake).
    import dataclasses
    cmac = dataclasses.replace(_eapol_hs(), key_version=3)
    assert nc.verify(cmac, "hashcat!") is False
    wl = tmp_path / "w.txt"
    wl.write_text("hashcat!\n", encoding="utf-8")
    res = nc.crack([cmac], str(wl))
    assert not res.cracked
    assert "CMAC" in res.detail or "hashcat" in res.detail


def test_crack_tries_multibyte_8_byte_passphrase(tmp_path):
    """WPA-PSK length is 8..63 BYTES, not code points. A passphrase that is 8 bytes but fewer than 8
    Unicode characters (a multibyte char) must still be tried — the old len(str) gate skipped it and
    falsely reported 'not in wordlist' even when the key was present."""
    pw = "señor12"                               # 'señor12': 7 code points, 8 UTF-8 bytes (ñ = 2)
    assert len(pw) == 7 and len(pw.encode("utf-8")) == 8
    ap = bytes.fromhex("fc690c158264")
    sta = bytes.fromhex("f4747f87f9f4")
    real_pmkid = nc.compute_pmkid(nc.pmk(pw, "netbyte"), ap, sta)
    hs = nc.Handshake(kind="pmkid", essid="netbyte", ap_mac=ap, sta_mac=sta, pmkid=real_pmkid)
    wl = tmp_path / "w.txt"
    wl.write_text("aaaaaaaa\n" + pw + "\n", encoding="utf-8")
    res = nc.crack([hs], str(wl))
    assert res.cracked and res.password == pw


def test_crack_uses_raw_essid_octets_as_salt(tmp_path):
    """A non-UTF-8 SSID must be salted with its EXACT octets, not a lossy str round-trip. essid_bytes
    carries the raw octets; the crack must succeed through those bytes even though the display str (a
    lossy decode) would produce a different, wrong PMK."""
    raw_essid = b"caf\xe9-net"                         # Latin-1 é (0xE9) — NOT valid UTF-8
    pw = "password1"
    ap = bytes.fromhex("fc690c158264")
    sta = bytes.fromhex("f4747f87f9f4")
    real_pmkid = nc.compute_pmkid(nc.pmk(pw, raw_essid), ap, sta)   # salt = the raw octets
    hs = nc.Handshake(kind="pmkid", essid=raw_essid.decode("utf-8", "replace"),
                      essid_bytes=raw_essid, ap_mac=ap, sta_mac=sta, pmkid=real_pmkid)
    # The lossy str salt would NOT reproduce the pmkid — proving the raw-octet path is load-bearing.
    assert nc.compute_pmkid(nc.pmk(pw, hs.essid), ap, sta) != real_pmkid
    wl = tmp_path / "w.txt"
    wl.write_text("nope1234\n" + pw + "\n", encoding="utf-8")
    res = nc.crack([hs], str(wl))
    assert res.cracked and res.password == pw


def test_native_cracks_supported_and_skips_cmac(tmp_path):
    # A mixed capture (one AES-CMAC v3 handshake + one crackable WPA2 v2 handshake) must still recover
    # the key from the supported one — the unsupported handshake is skipped, not a run-stopper.
    import dataclasses
    good = _eapol_hs()                                   # kv=2, real MIC, crackable with "hashcat!"
    cmac = dataclasses.replace(good, key_version=3)
    wl = tmp_path / "w.txt"
    wl.write_text("nope1234\nhashcat!\n", encoding="utf-8")
    res = nc.crack([cmac, good], str(wl))
    assert res.cracked and res.password == "hashcat!"
