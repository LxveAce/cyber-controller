"""End-to-end test for the capture parser (src/core/wpa_capture.py) -> native cracker.

Builds a synthetic classic-pcap (link type 105, bare 802.11) containing a beacon (BSSID -> ESSID) and
an EAPOL message-1 carrying a PMKID KDE, using the KNOWN-GOOD hashcat mode-22000 PMKID vector
(pmkid 4d4fe7aa…, essid "hashcat-essid", password "hashcat!"). If the parser extracts a handshake that
the native cracker then recovers, the parse -> crack path is proven with no external tool.
"""
from __future__ import annotations

import struct

from src.core import native_crack as nc
from src.core import wpa_capture as wc

_AP = bytes.fromhex("fc690c158264")
_STA = bytes.fromhex("f4747f87f9f4")
_PMKID = bytes.fromhex("4d4fe7aac3a2cecab195321ceb99a7d0")
_ESSID = "hashcat-essid"


def _pcap(frames, linktype=105):
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, linktype)  # global header
    for fr in frames:
        out += struct.pack("<IIII", 0, 0, len(fr), len(fr)) + fr           # record header + data
    return out


def _beacon(ap, ssid):
    fc = b"\x80\x00"                       # mgmt / beacon
    hdr = fc + b"\x00\x00" + b"\xff\xff\xff\xff\xff\xff" + ap + ap + b"\x00\x00"
    fixed = b"\x00" * 8 + b"\x64\x00" + b"\x01\x04"    # timestamp + interval + capabilities
    ssid_el = b"\x00" + bytes([len(ssid)]) + ssid.encode()
    return hdr + fixed + ssid_el


def _eapol_m1(ap, sta, anonce, pmkid):
    fc = b"\x08\x02"                       # data, FromDS=1
    dot11 = fc + b"\x00\x00" + sta + ap + sta + b"\x00\x00"     # a1=sta a2=ap(BSSID) a3=sta
    llc = b"\xaa\xaa\x03\x00\x00\x00\x88\x8e"
    kde = b"\xdd\x14\x00\x0f\xac\x04" + pmkid                   # PMKID KDE (vendor 00-0f-ac type 04)
    body = (b"\x02" + b"\x00\x8a" + b"\x00\x10" + b"\x00" * 8   # desc + key_info(ack) + keylen + replay
            + anonce + b"\x00" * 16 + b"\x00" * 8 + b"\x00" * 8  # nonce + iv + rsc + id
            + b"\x00" * 16 + struct.pack(">H", len(kde)) + kde)  # mic(zero) + kd_len + key_data
    eapol = b"\x01\x03" + struct.pack(">H", len(body)) + body
    return dot11 + llc + eapol


def test_parse_pmkid_capture_and_crack(tmp_path):
    cap = tmp_path / "test.pcap"
    cap.write_bytes(_pcap([_beacon(_AP, _ESSID), _eapol_m1(_AP, _STA, b"\x11" * 32, _PMKID)]))

    handshakes = wc.parse_capture(str(cap))
    assert handshakes, "parser should find the PMKID handshake"
    hs = handshakes[0]
    assert hs.kind == "pmkid"
    assert hs.essid == _ESSID
    assert hs.ap_mac == _AP and hs.sta_mac == _STA
    assert hs.pmkid == _PMKID

    # the extracted handshake must be crackable by the native cracker with the known password
    assert nc.verify(hs, "hashcat!")
    wl = tmp_path / "wl.txt"
    wl.write_text("nope1234\nhashcat!\n", encoding="utf-8")
    res = nc.crack(handshakes, str(wl))
    assert res.cracked and res.password == "hashcat!"


def test_parse_empty_or_garbage_is_soft(tmp_path):
    assert wc.parse_capture(str(tmp_path / "does-not-exist.pcap")) == []
    junk = tmp_path / "junk.pcap"
    junk.write_bytes(b"not a real capture at all")
    assert wc.parse_capture(str(junk)) == []


def test_pmkid_needs_essid(tmp_path):
    # EAPOL M1 with a PMKID but NO beacon -> no ESSID salt -> not crackable, so it's dropped.
    cap = tmp_path / "no_ssid.pcap"
    cap.write_bytes(_pcap([_eapol_m1(_AP, _STA, b"\x11" * 32, _PMKID)]))
    assert wc.parse_capture(str(cap)) == []


# ── 4-way-handshake (EAPOL-MIC) path ─────────────────────────────────────────────────────────────
# Rather than transcribe a fragile hard-coded MIC vector, we build a SELF-CONSISTENT M2: compute the
# real MIC with native_crack's own compute_mic over the mic-zeroed 802.1X frame, then splice it into
# the on-wire frame. verify() then re-derives that exact MIC — so a passing test proves the parser
# fed compute_mic the right bytes (correct trimming/zeroing). The crypto itself is separately pinned
# to hashcat's mode-22000 vector in test_native_crack.py.
_HS_AP = bytes.fromhex("6466b38ec3fc")
_HS_STA = bytes.fromhex("225edc49b7aa")
_HS_ESSID = "REGRESSION_NET"
_HS_PW = "regress!ok"                       # 10 chars — a valid WPA passphrase length
_HS_ANONCE = bytes(range(32))
_HS_SNONCE = bytes(range(32, 64))


def _eapol_key_pdu(nonce, mic, key_info):
    body = (b"\x02" + struct.pack(">H", key_info) + b"\x00\x10" + b"\x00" * 8   # desc+key_info+keylen+replay
            + nonce + b"\x00" * 16 + b"\x00" * 8 + b"\x00" * 8                  # nonce + iv + rsc + id
            + mic + b"\x00\x00")                                               # mic(16) + kd_len=0
    return b"\x01\x03" + struct.pack(">H", len(body)) + body                   # 802.1X hdr + body (plen=95)


def _real_m2_pdu(ap, sta):
    """The M2 EAPOL-Key PDU with a genuine MIC for _HS_PW (computed the same way verify() will)."""
    pmk = nc.pmk(_HS_PW, _HS_ESSID)
    zeroed = _eapol_key_pdu(_HS_SNONCE, b"\x00" * 16, 0x010a)   # MIC bit + pairwise + version 2
    tmp = nc.Handshake(kind="eapol", essid=_HS_ESSID, ap_mac=ap, sta_mac=sta,
                       anonce=_HS_ANONCE, snonce=_HS_SNONCE, eapol=zeroed, key_version=2)
    mic = nc.compute_mic(pmk, tmp)
    return _eapol_key_pdu(_HS_SNONCE, mic, 0x010a)


def _eapol_m1_frame(ap, sta):
    pdu = _eapol_key_pdu(_HS_ANONCE, b"\x00" * 16, 0x008a)   # ACK bit + pairwise + version 2, no MIC
    dot11 = b"\x08\x02" + b"\x00\x00" + sta + ap + sta + b"\x00\x00"  # data FromDS: a1=sta a2=ap a3=sta
    return dot11 + b"\xaa\xaa\x03\x00\x00\x00\x88\x8e" + pdu


def _eapol_m2_frame(ap, sta, pdu):
    dot11 = b"\x08\x01" + b"\x00\x00" + ap + sta + ap + b"\x00\x00"  # data ToDS: a1=ap a2=sta a3=ap
    return dot11 + b"\xaa\xaa\x03\x00\x00\x00\x88\x8e" + pdu


def test_eapol_handshake_with_trailing_fcs_still_cracks(tmp_path):
    # The headline native-crack bug: a monitor-mode M2 that carries a trailing 802.11 FCS must still
    # verify — the parser has to trim the frame to its 802.1X length so the FCS isn't hashed into the MIC.
    m2 = _eapol_m2_frame(_HS_AP, _HS_STA, _real_m2_pdu(_HS_AP, _HS_STA)) + b"\xde\xad\xbe\xef"  # +FCS
    cap = tmp_path / "fcs.pcap"
    cap.write_bytes(_pcap([_beacon(_HS_AP, _HS_ESSID), _eapol_m1_frame(_HS_AP, _HS_STA), m2]))
    eapol = [h for h in wc.parse_capture(str(cap)) if h.kind == "eapol"]
    assert eapol, "an M2 with a trailing FCS must still yield a handshake"
    assert eapol[0].essid == _HS_ESSID
    assert nc.verify(eapol[0], _HS_PW)   # only true if the FCS was trimmed from the MIC input


def test_eapol_handshake_before_its_beacon_resolves_essid(tmp_path):
    # ESSID-ordering bug: M1/M2 appear BEFORE the AP's beacon. The ESSID must still be resolved in the
    # final pass (like the PMKID path), not baked as "" at consume-time and dropped as saltless.
    cap = tmp_path / "order.pcap"
    cap.write_bytes(_pcap([_eapol_m1_frame(_HS_AP, _HS_STA),
                           _eapol_m2_frame(_HS_AP, _HS_STA, _real_m2_pdu(_HS_AP, _HS_STA)),
                           _beacon(_HS_AP, _HS_ESSID)]))   # beacon LAST
    eapol = [h for h in wc.parse_capture(str(cap)) if h.kind == "eapol"]
    assert eapol and eapol[0].essid == _HS_ESSID
    assert nc.verify(eapol[0], _HS_PW)


def test_truncated_pcapng_is_soft(tmp_path):
    # A truncated pcapng (SHB then an IDB whose block length leaves an empty body) must NOT crash the
    # parser with a struct.error — it returns [] (fail-soft), as the module promises.
    shb = (struct.pack("<II", 0x0A0D0D0A, 28) + b"\x4d\x3c\x2b\x1a"
           + struct.pack("<HH", 1, 0) + b"\xff" * 8 + struct.pack("<I", 28))
    idb = struct.pack("<II", 0x00000001, 12) + struct.pack("<I", 12)   # blen=12 -> empty body
    p = tmp_path / "trunc.pcapng"
    p.write_bytes(shb + idb)
    assert wc.parse_capture(str(p)) == []
