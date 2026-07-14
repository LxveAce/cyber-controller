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
    #: Raw SSID octets — the EXACT 802.11 PBKDF2 salt. ``essid`` (str) is a lossy decode for display
    #: only; a non-UTF-8 SSID salted with the re-encoded str would give a wrong PMK (silent false
    #: negative), so verify() prefers these bytes when present.
    essid_bytes: bytes = b""
    pmkid: bytes = b""
    anonce: bytes = b""
    snonce: bytes = b""
    eapol: bytes = b""          # the EAPOL-Key frame with the MIC field ZEROED
    mic: bytes = b""            # the captured MIC to match
    key_version: int = 2


def _octets(v) -> bytes:
    """Raw octets of a passphrase/SSID: bytes are fed verbatim (the exact 802.11 input, as hashcat/
    aircrack do with a wordlist); a str is re-encoded UTF-8 (canonical test vectors, or handshakes
    whose raw SSID octets weren't preserved)."""
    return bytes(v) if isinstance(v, (bytes, bytearray)) else str(v).encode("utf-8", "ignore")


def pmk(psk, essid) -> bytes:
    """The Pairwise Master Key: PBKDF2-HMAC-SHA1(passphrase, essid, 4096, 32). C-accelerated.

    Both *psk* and *essid* accept raw octets (bytes, fed verbatim) or a str (re-encoded UTF-8). A WPA
    passphrase and SSID are byte strings, so passing the exact octets avoids a lossy round-trip."""
    return hashlib.pbkdf2_hmac("sha1", _octets(psk), _octets(essid), 4096, 32)


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


def _verify_with_pmk(hs: "Handshake", the_pmk: bytes) -> bool:
    """True iff *the_pmk* (already derived for this handshake's salt) matches the captured PMKID/MIC.

    Split out from :func:`verify` so :func:`crack` can derive the PMK ONCE per distinct ESSID salt
    per candidate and reuse it across every handshake sharing that salt — the PMK is PBKDF2(psk,
    essid), so recomputing it per handshake wasted a 4096-round derivation on each duplicate-ESSID
    handshake (a multi-handshake same-ESSID capture multiplied crack time N-fold)."""
    if hs.kind == "pmkid":
        return hmac.compare_digest(compute_pmkid(the_pmk, hs.ap_mac, hs.sta_mac), hs.pmkid)
    if hs.kind == "eapol":
        # WPA (HMAC-MD5) and WPA2 (HMAC-SHA1) key MICs only. Key-descriptor versions 0/3 use
        # AES-128-CMAC (802.11w/PMF, WPA3-SHA256 AKMs) with a different KDF — verifying those with
        # SHA1 would be a silent false negative, so we decline honestly (never a fake match).
        if hs.key_version not in (1, 2):
            return False
        return hmac.compare_digest(compute_mic(the_pmk, hs), hs.mic)
    return False


def verify(hs: "Handshake", psk: "str | bytes") -> bool:
    """True iff *psk* reproduces this handshake's captured PMKID / MIC exactly (constant-time compare)."""
    # raw SSID octets are the correct PBKDF2 salt; the decoded str is a fallback
    return _verify_with_pmk(hs, pmk(psk, hs.essid_bytes or hs.essid))


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


def _display_psk(psk: bytes) -> "tuple[str, str]":
    """Render a matched passphrase (raw octets) for display, plus a note. Almost every WPA key is
    ASCII/UTF-8 and decodes exactly; a genuinely non-UTF-8 key can't be a clean str, so we show a
    lossy 'replace' rendering AND its exact hex in the note — never presenting mojibake as the key."""
    try:
        return psk.decode("utf-8"), ""
    except UnicodeDecodeError:
        return psk.decode("utf-8", "replace"), f" (non-UTF-8 key; exact bytes = {psk.hex()})"


#: A delimiter-free run longer than this can't be an 8..63-octet WPA passphrase; cap the read
#: buffer here so a newline-free wordlist can't be slurped whole into RAM.
_MAX_LINE_BYTES = 4096


def _iter_wordlist_lines(f, chunk_size: int = 1 << 16):
    r"""Yield candidate lines from a binary wordlist, split on CR, LF, or CRLF, with bounded memory.

    Python binary-mode iteration (``for raw in f``) splits on b"\n" ONLY, so a classic-Mac (lone-CR)
    or CR-separated wordlist would be yielded as ONE giant object — the real per-line passphrases
    never tested (a silent false negative that still reports a normal "key not in wordlist"), and a
    large newline-free file slurped whole into RAM. Reading in bounded chunks and normalizing b"\r"
    to b"\n" before splitting makes every line ending work; a delimiter-free run is capped at
    ``_MAX_LINE_BYTES`` (far above any WPA key) so memory stays bounded. hashcat/aircrack likewise
    treat a bare CR as a line ending, and a CRLF collapses to one boundary with an empty tail the
    caller's length gate drops."""
    buf = b""
    while True:
        chunk = f.read(chunk_size)
        if not chunk:
            break
        buf += chunk.replace(b"\r", b"\n")
        if b"\n" in buf:
            *lines, buf = buf.split(b"\n")
            for ln in lines:
                yield ln
        if len(buf) > _MAX_LINE_BYTES:
            buf = buf[-_MAX_LINE_BYTES:]
    if buf:
        yield buf


def crack(handshakes: list["Handshake"], wordlist_path: str, on_line: Optional[Line] = None,
          should_stop: Optional[Stop] = None, *, progress_every: int = 2000) -> NativeResult:
    """Try each passphrase in *wordlist_path* against the *handshakes* until one verifies or the list
    is exhausted. Returns a :class:`NativeResult`. WPA passphrases are 8..63 raw BYTES —
    shorter/longer candidates can't be a WPA key and are skipped for speed.

    Cooperative: calls ``should_stop()`` periodically so a UI can cancel; streams progress via
    ``on_line``. Reports a hit ONLY on an exact PMKID/MIC match (verify-never-fake)."""
    log: Line = on_line or (lambda *_a: None)
    stop: Stop = should_stop or (lambda: False)
    if not handshakes:
        return NativeResult(detail="no PMKID or handshake in this capture (nothing to crack)")
    # Drop EAPOL handshakes the native MIC path can't verify (AES-CMAC, key-descriptor v0/v3 —
    # 802.11w/PMF or WPA3-SHA256). Cracking them with SHA1 would silently report "not in wordlist"
    # even for the right key, so we surface the reason instead of misleading the operator.
    usable, skipped = [], 0
    for h in handshakes:
        if h.kind == "eapol" and h.key_version not in (1, 2):
            skipped += 1
        else:
            usable.append(h)
    if skipped:
        log(f"[native] skipping {skipped} AES-CMAC handshake(s) (802.11w/PMF or WPA3-SHA256) — the "
            f"native engine can't verify those; use the hashcat engine for them.")
    if not usable:
        return NativeResult(detail="only AES-CMAC (802.11w / WPA3-SHA256) handshakes here — "
                                   "use the hashcat engine for those")
    handshakes = usable
    ref = handshakes[0]
    tried = 0
    scanned = 0
    # Read the wordlist as RAW BYTES, not decoded text. A WPA passphrase is 8..63 OCTETS fed verbatim
    # to PBKDF2 (IEEE 802.11i), so hashcat/aircrack read the file as bytes too. Decoding it as UTF-8
    # first (errors="ignore") silently DROPPED invalid bytes from non-UTF-8 rockyou lines, corrupting
    # the candidate so the real key was never tried (a false "not in wordlist") — the passphrase-side
    # twin of the essid_bytes salt fix. A valid-UTF-8 line yields byte-identical results to before.
    with open(wordlist_path, "rb") as f:
        for raw in _iter_wordlist_lines(f):
            scanned += 1
            # Honor Stop + emit progress on lines SCANNED, not just valid candidates TRIED. A
            # wordlist of mostly out-of-range lines (<8 or >63 octets, e.g. a non-passphrase
            # file picked by mistake) advances `tried` rarely, so gating cancel/progress on
            # `tried` (old behavior) made the Stop button a no-op and the run look hung while
            # skipping them. Keying off `scanned` keeps cancellation responsive regardless of
            # candidate validity; `tried` stays the honest count.
            if scanned % progress_every == 0:
                if stop():
                    return NativeResult(tried=tried, detail="stopped")
                log(f"[native] scanned {scanned:,} lines, tried {tried:,} candidate(s)…")
            psk = raw.rstrip(b"\r\n")
            if not 8 <= len(psk) <= 63:  # WPA-PSK octet-length gate, exact on the raw bytes
                continue
            tried += 1
            # Derive the PMK once per distinct ESSID salt for this candidate and reuse it across
            # every handshake sharing that salt (see _verify_with_pmk) — a same-ESSID multi-
            # handshake capture no longer pays the 4096-round PBKDF2 once per handshake.
            pmk_by_salt: dict[bytes, bytes] = {}
            for hs in handshakes:
                salt = _octets(hs.essid_bytes or hs.essid)
                the_pmk = pmk_by_salt.get(salt)
                if the_pmk is None:
                    the_pmk = pmk(psk, salt)
                    pmk_by_salt[salt] = the_pmk
                if _verify_with_pmk(hs, the_pmk):
                    shown, note = _display_psk(psk)
                    log(f"[native] KEY FOUND for {hs.essid or _mac_str(hs.ap_mac)}: {shown}{note}")
                    return NativeResult(cracked=True, essid=hs.essid, bssid=_mac_str(hs.ap_mac),
                                        password=shown, tried=tried,
                                        detail="key recovered (native)" + note)
    return NativeResult(tried=tried, detail=f"key not in wordlist ({tried:,} candidates tried)",
                        essid=ref.essid, bssid=_mac_str(ref.ap_mac))
