r"""CC's own native WPA/WPA2 offline dictionary cracker — no external tool, nothing for AV to flag.

The existing pipeline (:mod:`src.core.crack_pipeline`) shells out to hcxpcapngtool + hashcat/aircrack.
Those are GPL binaries Windows Defender deletes as PUA, so they can't be shipped. WPA-PSK dictionary
cracking is, however, just standard public cryptography, and Python's ``hashlib.pbkdf2_hmac`` is the
C/OpenSSL implementation — so CC can do it itself, prepackaged and cross-platform, with no binary.

Same responsible posture as the rest of the crack feature:
* **Dictionary-only.** For each candidate passphrase we derive the PMK and check it against a captured
  PMKID or 4-way-handshake MIC. There is NO mask/brute-force path (that stays an owner decision).
* **Consent-gated + authorized-use only** — the UI shows :func:`crack_pipeline.consent_prompt_text`
  before any run; this module only does the math on a capture the operator supplied.
* **Verify-never-fake.** A passphrase is reported ONLY if it reproduces the captured PMKID / MIC exactly.

The crypto (IEEE 802.11i):
    PMK   = PBKDF2-HMAC-SHA1(psk, essid, 4096, 32)
    PMKID = HMAC-SHA1(PMK, "PMK Name" | AP_MAC | STA_MAC)[:16]
    PTK   = PRF-512(PMK, "Pairwise key expansion", min(AP,STA)|max(AP,STA)|min(An,Sn)|max(An,Sn))
    KCK   = PTK[:16];  MIC = HMAC-SHA1(KCK, eapol_with_mic_zeroed)[:16]  (WPA2)  /  HMAC-MD5 (WPA1)
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Callable, Optional

Line = Callable[[str], None]
Stop = Callable[[], bool]


@dataclass
class Handshake:
    """One crackable item extracted from a capture — a PMKID or a 4-way-handshake MIC.

    ``kind`` is ``"pmkid"`` or ``"eapol"``. ``essid`` is the network name (the PBKDF2 salt). MACs are
    6 raw bytes. PMKID path uses ``pmkid``; EAPOL path uses ``anonce``/``snonce``/``eapol``/``mic`` and
    ``key_version`` (1 = WPA/HMAC-MD5, 2 = WPA2/HMAC-SHA1)."""

    kind: str
    essid: str
    ap_mac: bytes
    sta_mac: bytes
    pmkid: bytes = b""
    anonce: bytes = b""
    snonce: bytes = b""
    eapol: bytes = b""          # the EAPOL-Key frame with the MIC field ZEROED
    mic: bytes = b""            # the captured MIC to match
    key_version: int = 2


def pmk(psk: str, essid: str) -> bytes:
    """The Pairwise Master Key: PBKDF2-HMAC-SHA1(passphrase, essid, 4096, 32). C-accelerated."""
    return hashlib.pbkdf2_hmac("sha1", psk.encode("utf-8", "ignore"),
                               essid.encode("utf-8", "ignore"), 4096, 32)


def compute_pmkid(the_pmk: bytes, ap_mac: bytes, sta_mac: bytes) -> bytes:
    """PMKID = HMAC-SHA1(PMK, b'PMK Name' + AP_MAC + STA_MAC) truncated to 16 bytes."""
    return hmac.new(the_pmk, b"PMK Name" + ap_mac + sta_mac, hashlib.sha1).digest()[:16]


def _prf512(the_pmk: bytes, label: bytes, data: bytes) -> bytes:
    """IEEE 802.11i PRF-512 → the first 64 bytes of the PTK (we only need KCK = first 16)."""
    out = b""
    i = 0
    while len(out) < 64:
        out += hmac.new(the_pmk, label + b"\x00" + data + bytes([i]), hashlib.sha1).digest()
        i += 1
    return out[:64]


def compute_mic(the_pmk: bytes, hs: "Handshake") -> bytes:
    """Derive the PTK/KCK for this handshake and return the MIC over its (mic-zeroed) EAPOL frame."""
    a, s = hs.ap_mac, hs.sta_mac
    an, sn = hs.anonce, hs.snonce
    b = min(a, s) + max(a, s) + min(an, sn) + max(an, sn)
    kck = _prf512(the_pmk, b"Pairwise key expansion", b)[:16]
    algo = hashlib.md5 if hs.key_version == 1 else hashlib.sha1
    return hmac.new(kck, hs.eapol, algo).digest()[:16]


def verify(hs: "Handshake", psk: str) -> bool:
    """True iff *psk* reproduces this handshake's captured PMKID / MIC exactly (constant-time compare)."""
    p = pmk(psk, hs.essid)
    if hs.kind == "pmkid":
        return hmac.compare_digest(compute_pmkid(p, hs.ap_mac, hs.sta_mac), hs.pmkid)
    if hs.kind == "eapol":
        return hmac.compare_digest(compute_mic(p, hs), hs.mic)
    return False


@dataclass
class NativeResult:
    """Outcome of a native dictionary run."""

    cracked: bool = False
    essid: str = ""
    bssid: str = ""
    password: str = ""
    tried: int = 0
    detail: str = ""


def _mac_str(mac: bytes) -> str:
    return ":".join(f"{b:02x}" for b in mac) if mac else ""


def crack(handshakes: list["Handshake"], wordlist_path: str, on_line: Optional[Line] = None,
          should_stop: Optional[Stop] = None, *, progress_every: int = 2000) -> NativeResult:
    """Try each passphrase in *wordlist_path* against the *handshakes* until one verifies or the list
    is exhausted. Returns a :class:`NativeResult`. WPA passphrases are 8..63 chars — shorter/longer
    candidates can't be a WPA key and are skipped for speed.

    Cooperative: calls ``should_stop()`` periodically so a UI can cancel; streams progress via
    ``on_line``. Reports a hit ONLY on an exact PMKID/MIC match (verify-never-fake)."""
    log: Line = on_line or (lambda *_a: None)
    stop: Stop = should_stop or (lambda: False)
    if not handshakes:
        return NativeResult(detail="no PMKID or handshake in this capture (nothing to crack)")
    ref = handshakes[0]
    tried = 0
    with open(wordlist_path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            psk = raw.rstrip("\r\n")
            if len(psk) < 8 or len(psk) > 63:
                continue
            tried += 1
            for hs in handshakes:
                if verify(hs, psk):
                    log(f"[native] KEY FOUND for {hs.essid or _mac_str(hs.ap_mac)}: {psk}")
                    return NativeResult(cracked=True, essid=hs.essid, bssid=_mac_str(hs.ap_mac),
                                        password=psk, tried=tried, detail="key recovered (native)")
            if tried % progress_every == 0:
                if stop():
                    return NativeResult(tried=tried, detail="stopped")
                log(f"[native] tried {tried:,} passphrases…")
    return NativeResult(tried=tried, detail=f"key not in wordlist ({tried:,} candidates tried)",
                        essid=ref.essid, bssid=_mac_str(ref.ap_mac))
