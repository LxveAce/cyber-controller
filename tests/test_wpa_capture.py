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
