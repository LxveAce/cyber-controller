r"""Beat 268 - wpa_capture 4-way Message-4 misclassified as Message-2 (cc-deep-audit-11 [4] MED).

The EAPOL-Key message discriminator in _consume_eapol inspected only the MIC (0x0100) and ACK
(0x0080) Key Information bits. Both M2 {MIC=1, ACK=0} and M4 {MIC=1, ACK=0} satisfy the M2 guard
`elif is_mic and not is_ack`, so a 4-way Message 4 fell into the M2 branch. Per IEEE 802.11i, M4's
Key Nonce is standard-mandated all-zero (the SNonce is not repeated in M4), so the parser appended a
Handshake with snonce=0 whose PRF-512 KCK can never match any passphrase - a permanently
unverifiable "handshake". Because a completed 4-way almost always includes M4 (sent unencrypted),
essentially every real capture yielded one good handshake PLUS a bogus zero-SNonce twin; and if M2
was lost in monitor mode, the M4-derived one was the ONLY handshake, so crack() falsely reported
"key not in wordlist" for a capture that carried no verifiable handshake at all.

Fix: in the M2 branch require a non-zero SNonce (uniquely identifies a real M2 vs M4) AND the
pairwise Key Type bit (0x0008), so M4 and any group frame are dropped.

Discriminating (fail on buggy HEAD, pass on the fix):
  - test_m4_zero_snonce_not_emitted: an M1+M4-only capture yields NO eapol handshake on the fix;
    HEAD emits one bogus zero-SNonce handshake.
  - test_full_4way_yields_only_real_m2: an M1+M2+M4 capture yields EXACTLY the real M2 (which
    cracks) on the fix; HEAD yields two (the good M2 + the bogus M4 twin).
Guard (passes on both): a normal M1+M2 capture still yields a crackable handshake.
"""
from __future__ import annotations

import struct

import pytest

from src.core import native_crack as nc
from src.core import wpa_capture as wc

_ESSID = "test-net-4way"
_PW = "correcthorse"
_AP = bytes.fromhex("aabbccddeeff")
_STA = bytes.fromhex("112233445566")
_ANONCE = bytes(range(32))          # non-zero ANonce
_SNONCE = bytes(range(32, 64))      # non-zero SNonce (a real M2)
_ZERO_NONCE = b"\x00" * 32          # M4's standard-mandated all-zero Key Nonce


def _pcap(frames, linktype=105):
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, linktype)
    for fr in frames:
        out += struct.pack("<IIII", 0, 0, len(fr), len(fr)) + fr
    return out


def _beacon(ap, ssid):
    hdr = b"\x80\x00" + b"\x00\x00" + b"\xff\xff\xff\xff\xff\xff" + ap + ap + b"\x00\x00"
    fixed = b"\x00" * 8 + b"\x64\x00" + b"\x01\x04"
    return hdr + fixed + b"\x00" + bytes([len(ssid)]) + ssid.encode()


def _key_pdu(nonce, mic, key_info):
    body = (b"\x02" + struct.pack(">H", key_info) + b"\x00\x10" + b"\x00" * 8
            + nonce + b"\x00" * 16 + b"\x00" * 8 + b"\x00" * 8
            + mic + b"\x00\x00")
    return b"\x01\x03" + struct.pack(">H", len(body)) + body


def _m1_frame(ap, sta):
    pdu = _key_pdu(_ANONCE, b"\x00" * 16, 0x008a)   # ACK + pairwise + v2, no MIC
    dot11 = b"\x08\x02" + b"\x00\x00" + sta + ap + sta + b"\x00\x00"   # FromDS
    return dot11 + b"\xaa\xaa\x03\x00\x00\x00\x88\x8e" + pdu


def _sta_to_ap_frame(ap, sta, pdu):
    dot11 = b"\x08\x01" + b"\x00\x00" + ap + sta + ap + b"\x00\x00"    # ToDS (M2 and M4 direction)
    return dot11 + b"\xaa\xaa\x03\x00\x00\x00\x88\x8e" + pdu


def _real_m2_pdu(ap, sta):
    """A genuine M2 PDU whose MIC verifies for _PW (computed the way verify() will)."""
    the_pmk = nc.pmk(_PW, _ESSID)
    zeroed = _key_pdu(_SNONCE, b"\x00" * 16, 0x010a)   # MIC + pairwise + v2
    tmp = nc.Handshake(kind="eapol", essid=_ESSID, ap_mac=ap, sta_mac=sta,
                       anonce=_ANONCE, snonce=_SNONCE, eapol=zeroed, key_version=2)
    mic = nc.compute_mic(the_pmk, tmp)
    return _key_pdu(_SNONCE, mic, 0x010a)


def _m4_pdu():
    # M4: {MIC=1, ACK=0}, Secure=1, pairwise, all-zero Key Nonce. The MIC value is irrelevant - an
    # M4-derived handshake can never verify, which is exactly why it must not be emitted.
    return _key_pdu(_ZERO_NONCE, b"\x11" * 16, 0x030a)   # Secure + MIC + pairwise + v2


def _parse(tmp_path, frames) -> list:
    cap = tmp_path / "cap.pcap"
    cap.write_bytes(_pcap(frames))
    return [h for h in wc.parse_capture(str(cap)) if h.kind == "eapol"]


def test_m4_zero_snonce_not_emitted(tmp_path):
    """[4] discriminator: M1+M4 only -> the fix emits NO handshake; HEAD emits a bogus one."""
    eapols = _parse(tmp_path, [_beacon(_AP, _ESSID), _m1_frame(_AP, _STA),
                               _sta_to_ap_frame(_AP, _STA, _m4_pdu())])

    assert eapols == [], "an all-zero-SNonce M4 must not become an unverifiable handshake"


def test_full_4way_yields_only_real_m2(tmp_path):
    """[4] discriminator: M1+M2+M4 -> the fix keeps only the real (crackable) M2; HEAD keeps two."""
    eapols = _parse(tmp_path, [_beacon(_AP, _ESSID), _m1_frame(_AP, _STA),
                               _sta_to_ap_frame(_AP, _STA, _real_m2_pdu(_AP, _STA)),
                               _sta_to_ap_frame(_AP, _STA, _m4_pdu())])

    assert len(eapols) == 1, "only the real M2 handshake should survive (not the bogus M4 twin)"
    assert eapols[0].snonce == _SNONCE
    assert nc.verify(eapols[0], _PW), "the surviving handshake must be the crackable M2"


def test_real_m2_still_yields_crackable_handshake(tmp_path):
    """Guard (passes on HEAD + fix): a normal M1+M2 capture still yields a crackable handshake."""
    eapols = _parse(tmp_path, [_beacon(_AP, _ESSID), _m1_frame(_AP, _STA),
                               _sta_to_ap_frame(_AP, _STA, _real_m2_pdu(_AP, _STA))])

    assert len(eapols) == 1
    assert nc.verify(eapols[0], _PW)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
