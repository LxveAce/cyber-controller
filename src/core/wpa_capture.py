r"""Parse a Wi-Fi capture (.pcap / .pcapng) into crackable WPA handshakes for :mod:`native_crack`.

This is the one job CC needs from hcxpcapngtool (which has no Windows binary), reimplemented in pure
Python: pull **PMKIDs** and **4-way-handshake MICs** — plus the **ESSID** from beacons / probe-responses
(the PBKDF2 salt) — out of a capture so the native cracker has something to crack. No external tool.

Scope: classic pcap (either byte order) + pcapng; link types RADIOTAP (127), bare 802.11 (105), and
PPI (192)/AVS(163) headers are length-skipped best-effort. Only what's needed for WPA/WPA2-PSK recovery
is decoded — this is a cracker feed, not a general 802.11 dissector, and it fails soft (a frame it can't
parse is skipped, never fatal).
"""
from __future__ import annotations

import struct
from typing import Iterator

from .native_crack import Handshake

# pcap link-layer types we understand enough to reach the 802.11 MAC header.
_LT_DOT11 = 105
_LT_RADIOTAP = 127
_LT_PPI = 192
_EAPOL_ETHERTYPE = 0x888E


def _iter_records(data: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield ``(linktype, frame_bytes)`` for each packet, handling classic pcap + pcapng."""
    if len(data) < 4:
        return
    magic = data[:4]
    if magic in (b"\xa1\xb2\xc3\xd4", b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\x3c\x4d", b"\x4d\x3c\xb2\xa1"):
        yield from _iter_pcap(data)
    elif magic == b"\x0a\x0d\x0d\x0a":
        yield from _iter_pcapng(data)


def _iter_pcap(data: bytes) -> Iterator[tuple[int, bytes]]:
    le = data[:4] in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1")
    end = "<" if le else ">"
    if len(data) < 24:
        return
    linktype = struct.unpack(end + "I", data[20:24])[0]
    off = 24
    n = len(data)
    while off + 16 <= n:
        _ts, _tu, incl, _orig = struct.unpack(end + "IIII", data[off:off + 16])
        off += 16
        if incl > n - off:
            break
        yield linktype, data[off:off + incl]
        off += incl


def _iter_pcapng(data: bytes) -> Iterator[tuple[int, bytes]]:
    off = 0
    n = len(data)
    linktypes: dict[int, int] = {}   # interface_id -> linktype
    iface_count = 0
    end = "<"  # refined from the SHB byte-order magic
    while off + 12 <= n:
        btype, blen = struct.unpack(end + "II", data[off:off + 8])
        if btype == 0x0A0D0D0A:  # Section Header Block — read its byte-order magic
            bom = data[off + 8:off + 12]
            end = "<" if bom == b"\x4d\x3c\x2b\x1a" else ">"
            btype, blen = struct.unpack(end + "II", data[off:off + 8])
        if blen < 12 or off + blen > n:
            break
        body = data[off + 8:off + blen - 4]
        # A truncated/malformed block can leave `body` shorter than the fields we read — guard every
        # unpack so a bad pcapng is skipped, never fatal (the module's fail-soft contract).
        if btype == 0x00000001:  # Interface Description Block
            if len(body) >= 2:
                lt = struct.unpack(end + "H", body[0:2])[0]
                linktypes[iface_count] = lt
            iface_count += 1  # keep interface_id alignment even if this IDB is short
        elif btype == 0x00000006:  # Enhanced Packet Block
            # EPB leading fields (5x u32 = 20 bytes) BEFORE packet data: Interface ID, Timestamp High,
            # Timestamp Low, Captured Packet Length, Original Packet Length. Packet data starts at
            # body[20:], NOT body[16:] — skipping only four fields prefixed every frame with the 4-byte
            # Original-Length value and shifted it left by 4, so no 802.11 header aligned and a real
            # pcapng (hcxdumptool/Wireshark, the primary modern format) yielded zero handshakes.
            if len(body) >= 20:
                iface_id, _th, _tl, caplen, _origlen = struct.unpack(end + "IIIII", body[0:20])
                frame = body[20:20 + caplen]
                yield linktypes.get(iface_id, _LT_DOT11), frame
        elif btype == 0x00000003:  # Simple Packet Block
            if len(body) >= 4:
                yield linktypes.get(0, _LT_DOT11), body[4:]
        off += blen


def _to_dot11(linktype: int, frame: bytes) -> bytes:
    """Strip any radiotap/PPI header, returning the bare 802.11 frame (or b'' if not applicable)."""
    if linktype == _LT_DOT11:
        return frame
    if linktype == _LT_RADIOTAP:
        if len(frame) < 4:
            return b""
        rt_len = struct.unpack("<H", frame[2:4])[0]  # radiotap length is always little-endian
        return frame[rt_len:] if rt_len <= len(frame) else b""
    if linktype == _LT_PPI:
        if len(frame) < 4:
            return b""
        ppi_len = struct.unpack("<H", frame[2:4])[0]
        return frame[ppi_len:] if ppi_len <= len(frame) else b""
    return b""


def _dot11_addrs(f: bytes) -> tuple[int, int, bytes, bytes, bytes, int]:
    """Return (ftype, subtype, addr1, addr2, addr3, hdr_len) for an 802.11 frame, or hdr_len=0 if too
    short. addr1/2/3 are 6 raw bytes each."""
    if len(f) < 24:
        return (0, 0, b"", b"", b"", 0)
    fc = f[0]
    ftype = (fc >> 2) & 0x3
    subtype = (fc >> 4) & 0xF
    to_ds = f[1] & 0x01
    from_ds = (f[1] >> 1) & 0x01
    hdr = 24
    if to_ds and from_ds:
        hdr += 6  # addr4 present
    if ftype == 2 and (subtype & 0x08):  # QoS data
        hdr += 2
    return (ftype, subtype, f[4:10], f[10:16], f[16:22], hdr)


def _ssid_from_beacon(f: bytes, hdr_len: int) -> bytes:
    """Extract the raw SSID octets from a beacon / probe-response management body (skips the 12-byte
    fixed params: timestamp+beacon-interval+capabilities). SSID is element id 0. Returns the EXACT
    octets — they are the PBKDF2 salt, so they must not be lossily decoded here; the caller derives a
    display str separately."""
    body = f[hdr_len + 12:]
    i = 0
    while i + 2 <= len(body):
        eid, elen = body[i], body[i + 1]
        val = body[i + 2:i + 2 + elen]
        if eid == 0:  # SSID
            return val
        i += 2 + elen
    return b""


def _pmkid_from_keydata(kd: bytes) -> bytes:
    """Find a PMKID KDE (vendor-specific 0xDD, OUI 00-0F-AC, data-type 0x04) in EAPOL M1 key data."""
    i = 0
    while i + 2 <= len(kd):
        eid, elen = kd[i], kd[i + 1]
        val = kd[i + 2:i + 2 + elen]
        if eid == 0xDD and len(val) >= 4 and val[:3] == b"\x00\x0f\xac" and val[3] == 0x04:
            pmkid = val[4:20]
            if len(pmkid) == 16 and pmkid != b"\x00" * 16:
                return pmkid
        i += 2 + elen
    return b""


def parse_capture(path: str) -> list[Handshake]:
    """Parse *path* into a list of crackable :class:`Handshake` (PMKIDs first, then EAPOL MICs).

    Best-effort + fail-soft: an unreadable/odd frame is skipped, never fatal. ESSIDs are resolved from
    beacons/probe-responses by BSSID; a handshake whose ESSID can't be found is dropped (no salt = not
    crackable). PMKID needs only EAPOL message 1; the EAPOL-MIC path pairs message 1 (ANonce) with
    message 2 (SNonce + MIC) for the same AP/STA."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return []

    ssids: dict[bytes, bytes] = {}                         # BSSID -> raw SSID octets (the PBKDF2 salt)
    pmkids: list[tuple[bytes, bytes, bytes]] = []          # (ap, sta, pmkid)
    m1: dict[tuple[bytes, bytes], bytes] = {}              # (ap,sta) -> anonce
    eapol_hs: list[Handshake] = []

    for linktype, frame in _iter_records(data):
        f = _to_dot11(linktype, frame)
        if len(f) < 24:
            continue
        ftype, subtype, a1, a2, a3, hdr = _dot11_addrs(f)
        if hdr == 0:
            continue
        # Management beacon (8) / probe-response (5): learn BSSID -> SSID.
        if ftype == 0 and subtype in (8, 5):
            ssid = _ssid_from_beacon(f, hdr)
            if ssid:
                ssids[a3] = ssid  # addr3 = BSSID in mgmt frames
            continue
        if ftype != 2:  # only data frames carry EAPOL
            continue
        payload = f[hdr:]
        # LLC/SNAP: AA AA 03 00 00 00 <ethertype>. EAPOL ethertype 0x888E.
        if len(payload) < 8 or payload[:6] != b"\xaa\xaa\x03\x00\x00\x00":
            continue
        if struct.unpack(">H", payload[6:8])[0] != _EAPOL_ETHERTYPE:
            continue
        _consume_eapol(f, payload[8:], ssids, pmkids, m1, eapol_hs)

    out: list[Handshake] = []
    for ap, sta, pmkid in pmkids:
        raw = ssids.get(ap, b"")
        if raw:
            out.append(Handshake(kind="pmkid", essid=raw.decode("utf-8", "replace"), essid_bytes=raw,
                                 ap_mac=ap, sta_mac=sta, pmkid=pmkid))
    # Resolve each EAPOL handshake's ESSID now that ALL beacons have been seen — a handshake captured
    # before its AP's beacon still gets its network name (without this it'd be dropped as saltless).
    for hs in eapol_hs:
        raw = ssids.get(hs.ap_mac, b"")
        hs.essid_bytes = raw                       # exact octets = the PBKDF2 salt (crack path)
        hs.essid = raw.decode("utf-8", "replace")  # display only
        if raw:
            out.append(hs)
    return out


def _consume_eapol(f: bytes, eapol: bytes, ssids, pmkids, m1, eapol_hs) -> None:
    """Decode one EAPOL-Key frame and accumulate PMKID / handshake state. Fail-soft."""
    # The fixed EAPOL-Key body is 95 bytes (desc 1 + keyinfo 2 + keylen 2 + replay 8 + nonce 32 +
    # iv 16 + rsc 8 + keyid 8 + MIC 16 + keydatalen 2), so the full frame is >= 99 (4-byte 802.1X
    # header + body). Requiring only 95/91 left the reads below 4 bytes short: mic = body[77:93]
    # under-read to 14-15 bytes, and the M2 zeroed[81:97] = 16 zero bytes GREW a short bytearray,
    # producing a corrupt-but-structurally-complete "handshake" that can never verify (even the
    # correct passphrase reports not-found). Require the full fixed body so a truncated frame is
    # skipped as unparseable instead of laundered into a fake handshake.
    if len(eapol) < 99 or eapol[1] != 3:   # 802.1X type 3 = EAPOL-Key; 99 = 4 hdr + 95 fixed body
        return
    body = eapol[4:]                        # skip 802.1X header (version,type,length[2])
    if len(body) < 95:
        return
    key_info = struct.unpack(">H", body[1:3])[0]
    nonce = body[13:45]
    mic = body[77:93]
    kd_len = struct.unpack(">H", body[93:95])[0] if len(body) >= 95 else 0
    key_data = body[95:95 + kd_len]

    to_ds = f[1] & 0x01
    from_ds = (f[1] >> 1) & 0x01
    a1, a2 = f[4:10], f[10:16]
    # Message 1 (AP->STA): FromDS=1 -> ap=addr2, sta=addr1. Message 2 (STA->AP): ToDS=1 -> ap=addr1.
    if from_ds and not to_ds:
        ap, sta = a2, a1
    elif to_ds and not from_ds:
        ap, sta = a1, a2
    else:
        ap, sta = a2, a1

    is_mic = bool(key_info & 0x0100)
    is_ack = bool(key_info & 0x0080)
    kv = key_info & 0x07

    if is_ack and not is_mic:            # message 1 (ANonce, maybe PMKID)
        m1[(ap, sta)] = nonce
        pmkid = _pmkid_from_keydata(key_data)
        if pmkid:
            pmkids.append((ap, sta, pmkid))
    elif is_mic and not is_ack:          # message 2 (SNonce + MIC over this frame)
        anonce = m1.get((ap, sta))
        if anonce:
            # Trim to the 802.1X PDU length (4-byte header + declared body). A monitor-mode capture
            # often appends the 4-byte 802.11 FCS (or pad bytes), which are NOT part of the MIC input
            # (hashing them in makes the correct passphrase never verify). Fall back to the full frame
            # if the declared length is implausible (below the MIC end, or past the captured bytes).
            plen = struct.unpack(">H", eapol[2:4])[0]
            frame = eapol[:4 + plen] if 95 <= plen <= len(eapol) - 4 else eapol
            zeroed = bytearray(frame)
            zeroed[81:97] = b"\x00" * 16  # MIC field within the EAPOL frame (4 hdr + 77)
            # ESSID (the PBKDF2 salt) is resolved in parse_capture's final pass, so a beacon that
            # appears AFTER this handshake still names the network (matches the PMKID path).
            eapol_hs.append(Handshake(
                kind="eapol", essid="", ap_mac=ap, sta_mac=sta,
                anonce=anonce, snonce=nonce, eapol=bytes(zeroed), mic=mic, key_version=kv))
