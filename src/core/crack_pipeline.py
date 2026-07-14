r"""WPA/WPA2 handshake + PMKID offline-crack pipeline (capture -> convert -> crack -> report).

This is the host-side *offline* half of the Wi-Fi audit flow. The *capture* half already exists
as firmware-CLI macros (``cc_marauder_pmkid_sniff.json`` for a passive PMKID/handshake sniff, and
the targeted-deauth template for a deauth-assisted 4-way-handshake grab). Those write a
``.pcap``/``.pcapng`` to the capturing device's SD card. This module takes that capture file and
runs the standard offline recovery chain against it:

    .pcapng --hcxpcapngtool--> .hc22000 --hashcat -m 22000 -a 0--> PSK (or "not in wordlist")
                                         --aircrack-ng -w--------> PSK   (CPU fallback)

Deliberate honesty / reliability invariants (load-bearing, not decoration):

* **Not bundled; fetched or found.** hcxtools (``hcxpcapngtool``), hashcat and aircrack-ng are GPL and
  are NOT vendored into CC. CC *shells out* to whatever is on PATH or in the CC tools dir; where an
  official prebuilt binary exists (aircrack-ng on Windows) the in-app installer
  (:mod:`src.core.tool_installer`) can fetch + verify it on demand, and everything else gets honest
  install guidance. :func:`detect_tools` reports exactly what is present; a missing tool yields an
  honest "install it" message, never a fake success. CC ships no attack binaries.
* **Dictionary-only.** The only attack mode built here is hashcat straight mode (``-a 0``) /
  aircrack-ng wordlist mode. Brute-force / mask (``-a 3``) is intentionally NOT built: it is a
  different legal + expectations conversation (hours-to-never runtimes, easy to misrepresent), so
  it is an explicit owner decision, not a silent default. The UI copy says "dictionary attack".
* **Consent-gated.** Recovering a PSK is only lawful against a network you own or have written
  authorization to test. :func:`consent_prompt_text` is the per-run affirmation the UI must show
  and the operator must accept before a crack launches -- on top of the app's one-time legal
  disclaimer (:mod:`src.core.safety`). The capture step's deauth is separately gated ``lab-only``
  by the safety engine; this module gates the *use* of the captured material.
* **Verify-never-fake.** "No handshake/PMKID in this capture" (0 extractable hashes) and "key not
  in this wordlist" are first-class honest negatives, surfaced plainly. A crack that finds nothing
  is reported as finding nothing.

Structure: the pure pieces -- argv construction, output parsing, tool-name resolution -- are
unit-testable with no hardware and no tools installed (they only shape strings/argv). The
subprocess orchestration (:func:`convert_capture`, :func:`run_hashcat`, :func:`run_aircrack`) is a
thin, best-effort layer. No ``shell=True``, argv lists only, and every user-supplied path is
validated before it reaches a subprocess.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Optional

Line = Callable[[str], None]

# -- Tool identity ----------------------------------------------------

#: Converter: hcxtools' pcapng -> hashcat-22000 extractor (modern replacement for hcxpcaptool).
CONVERTER = "hcxpcapngtool"
#: GPU cracker. WPA-PBKDF2-PMKID+EAPOL is hashcat mode 22000 (unified PMKID *and* EAPOL format).
HASHCAT = "hashcat"
#: CPU fallback cracker (no GPU / no hcxtools) -- aircrack reads the pcap directly.
AIRCRACK = "aircrack-ng"

#: hashcat hash-mode for WPA/WPA2 (PMKID + EAPOL, the .hc22000 format). This module is 22000-only.
HASHCAT_MODE_WPA = "22000"

#: Capture extensions we accept as crack input.
CAPTURE_EXTS = (".pcapng", ".pcap", ".cap")


@dataclass
class ToolStatus:
    """Presence + version of one external tool. ``path`` is None when it isn't on PATH."""

    name: str
    path: Optional[str] = None
    version: str = ""

    @property
    def present(self) -> bool:
        return bool(self.path)


@dataclass
class CrackResult:
    """Outcome of a crack run. A *found* key is the only success; the rest are honest negatives."""

    cracked: bool = False
    ssid: str = ""
    bssid: str = ""
    password: str = ""
    #: How many crackable hashes the converter extracted (0 == nothing to crack).
    hashes_extracted: int = 0
    #: Human-readable outcome ("key not in wordlist", "no handshake in capture", tool error).
    detail: str = ""
    #: Extra creds if a multi-network capture cracked more than one (dicts of ssid/bssid/password).
    extra: list[dict] = field(default_factory=list)


# -- Tool detection (thin: shutil.which + `--version`) ----------------

def _probe_version(path: str, args: tuple[str, ...]) -> str:
    """Best-effort single-line version string. Never raises; '' if the tool won't answer."""
    try:
        r = subprocess.run([path, *args], capture_output=True, text=True, timeout=8)
    except Exception:  # noqa: BLE001 -- a version probe must never break detection
        return ""
    blob = (r.stdout or "") + (r.stderr or "")
    for ln in blob.splitlines():
        if ln.strip():
            return ln.strip()[:80]
    return ""


def _installed_fallback(name: str) -> Optional[str]:
    """A tool the operator installed via the in-app installer (or hand-dropped) into the CC tools dir,
    consulted after PATH so an installed aircrack-ng is found even when it isn't globally on PATH. The
    lazy import avoids a load-time cycle with tool_installer (which imports these tool names from here)."""
    try:
        from .tool_installer import installed_tools
        return installed_tools().get(name)
    except Exception:  # noqa: BLE001 — the fallback must never break detection
        return None


def detect_tools() -> dict[str, ToolStatus]:
    """Resolve the three external tools -> {name: ToolStatus}. Prefers PATH, then falls back to the CC
    tools dir (``~/.cyber-controller/tools`` — where the in-app installer puts them). No tool is required
    present; the caller decides what it can do with what's installed (hashcat OR aircrack is enough to
    crack; hcxpcapngtool is only needed for the hashcat path)."""
    probes = {
        CONVERTER: ("--version",),
        HASHCAT: ("--version",),
        # aircrack-ng has no --version; --help prints the banner with the version.
        AIRCRACK: ("--help",),
    }
    out: dict[str, ToolStatus] = {}
    for name, vargs in probes.items():
        path = shutil.which(name) or _installed_fallback(name)
        version = _probe_version(path, vargs) if path else ""
        out[name] = ToolStatus(name=name, path=path, version=version)
    return out


def available_backends(tools: dict[str, ToolStatus]) -> list[str]:
    """Which end-to-end crack backends are usable.

    * ``"native"`` is CC's own pure-Python cracker (:mod:`src.core.native_crack`) — ALWAYS available,
      needs no external tool, nothing for AV to flag. Listed first so Crack Lab works out of the box.
    * ``"hashcat"`` needs BOTH hcxpcapngtool (to make the .hc22000) AND hashcat (GPU-fast when present).
    * ``"aircrack"`` needs only aircrack-ng (it reads the .pcap directly).
    The external backends are optional accelerators layered on top of the always-present native one."""
    def have(n: str) -> bool:
        return tools.get(n, ToolStatus(n)).present

    backs: list[str] = ["native"]
    if have(CONVERTER) and have(HASHCAT):
        backs.append("hashcat")
    if have(AIRCRACK):
        backs.append("aircrack")
    return backs


def run_native(capture: str, wordlist: str, on_line: Line,
               bssid: str = "", should_stop: Optional[Callable[[], bool]] = None) -> CrackResult:
    """CC's OWN dictionary crack — parse the capture + try the wordlist natively, no external tool.

    Reads PMKIDs / 4-way-handshake MICs (+ the ESSID) straight out of the ``.pcap``/``.pcapng`` and
    checks each candidate passphrase against them in pure Python. Same honest posture as the rest:
    dictionary-only, verify-never-fake. Raises ValueError on bad input."""
    from src.core import native_crack, wpa_capture
    validate_capture(capture)
    validate_wordlist(wordlist)
    handshakes = wpa_capture.parse_capture(capture)
    if bssid:
        want = bssid.lower().replace(":", "").replace("-", "")
        filtered = [h for h in handshakes if h.ap_mac.hex() == want]
        if not filtered:
            # Honest negative: the operator targeted a specific BSSID that has no handshake in this
            # capture (a typo, or its handshake wasn't captured). Do NOT silently fall back to ALL
            # handshakes — that would crack a DIFFERENT, non-targeted AP within the capture. aircrack's
            # -b targets strictly; match that.
            on_line(f"[native] no handshake for BSSID {bssid} in this capture — nothing to crack")
            return CrackResult(cracked=False, hashes_extracted=0,
                               detail=f"no handshake for BSSID {bssid} in this capture")
        handshakes = filtered
    on_line(f"[native] {len(handshakes)} crackable PMKID/handshake(s) in this capture")
    res = native_crack.crack(handshakes, wordlist, on_line, should_stop)
    return CrackResult(cracked=res.cracked, ssid=res.essid, bssid=res.bssid, password=res.password,
                       hashes_extracted=len(handshakes),
                       detail=res.detail if not res.cracked else "key recovered (native)")


# -- Input validation -------------------------------------------------

def validate_capture(path: str) -> str:
    """Return *path* if it is an existing capture file with an accepted extension; else ValueError.
    Guards the subprocess layer against a missing file or a wrong-type input passed to a tool."""
    if not isinstance(path, str) or not path:
        raise ValueError("no capture file given")
    if not os.path.isfile(path):
        raise ValueError(f"capture file not found: {path!r}")
    if os.path.splitext(path)[1].lower() not in CAPTURE_EXTS:
        raise ValueError(f"not a capture file (expected {'/'.join(CAPTURE_EXTS)}): {path!r}")
    return path


#: A prebuilt hashcat hashfile — a valid crack INPUT, but only for the hashcat engine (native/aircrack
#: read a raw capture, not a prehashed file), so it isn't in CAPTURE_EXTS.
HASHFILE_EXT = ".hc22000"


def validate_crack_input(path: str, backend: str) -> str:
    """Validate the crack input for *backend*. A prebuilt ``.hc22000`` hashfile is a valid input but ONLY
    for the hashcat engine — native/aircrack parse a raw capture, so a hashfile can't feed them. Any other
    input must be a ``.pcapng``/``.pcap``/``.cap`` capture (delegates to :func:`validate_capture`). Raises
    ValueError on a bad file or an input/engine mismatch.

    Without this, the UI advertised ``.hc22000`` everywhere (Browse filter, placeholder, double-click
    loader) but ``validate_capture`` rejected it, so the prebuilt-hashfile path was completely unreachable.
    """
    if not isinstance(path, str) or not path:
        raise ValueError("no capture file given")
    if os.path.splitext(path)[1].lower() == HASHFILE_EXT:
        if backend != "hashcat":
            raise ValueError(
                "a prebuilt .hc22000 hashfile can only be cracked with the hashcat engine — "
                "select 'hashcat' as the engine (native/aircrack read a raw .pcap capture instead).")
        if not os.path.isfile(path):
            raise ValueError(f"hashfile not found: {path!r}")
        return path
    return validate_capture(path)


def validate_wordlist(path: str) -> str:
    """Return *path* if it is an existing, non-empty file; else ValueError. A dictionary attack with
    a missing/empty wordlist would 'complete' having tried nothing -- honest tools refuse that."""
    if not isinstance(path, str) or not path:
        raise ValueError("no wordlist given (a dictionary attack needs a wordlist)")
    if not os.path.isfile(path):
        raise ValueError(f"wordlist not found: {path!r}")
    if os.path.getsize(path) == 0:
        raise ValueError(f"wordlist is empty: {path!r}")
    return path


# -- argv construction (pure) -----------------------------------------

def build_convert_argv(capture: str, out_hc22000: str, converter: str = CONVERTER) -> list[str]:
    """hcxpcapngtool argv: extract PMKID/EAPOL from *capture* into the 22000 file *out_hc22000*."""
    return [converter, "-o", out_hc22000, capture]


def build_hashcat_argv(
    hash_file: str,
    wordlist: str,
    hashcat: str = HASHCAT,
    *,
    show: bool = False,
    extra_args: Optional[list[str]] = None,
) -> list[str]:
    """hashcat argv for a WPA dictionary attack (mode 22000, ``-a 0`` straight).

    ``show=True`` builds the ``--show`` invocation that prints already-cracked hashes (from the
    potfile) instead of launching a run -- how results are read back after a crack. Only dictionary
    mode is ever constructed; there is no code path that emits ``-a 3`` (mask/brute) -- a deliberate
    owner-gated omission, not an oversight."""
    argv = [hashcat, "-m", HASHCAT_MODE_WPA, "-a", "0", hash_file, wordlist]
    if show:
        argv.append("--show")
    if extra_args:
        argv.extend(extra_args)
    return argv


def build_aircrack_argv(
    capture: str,
    wordlist: str,
    aircrack: str = AIRCRACK,
    *,
    bssid: str = "",
) -> list[str]:
    """aircrack-ng argv for a WPA dictionary attack read straight from the *capture* (no convert).
    An optional *bssid* narrows a multi-network capture to one AP so the run isn't ambiguous."""
    argv = [aircrack, "-w", wordlist]
    if bssid:
        argv += ["-b", bssid]
    argv.append(capture)
    return argv


# -- output parsing (pure) --------------------------------------------

def count_extractable(hc22000_text: str) -> int:
    """How many crackable WPA hashes are in an .hc22000 file's text (each ``WPA*..`` line is one).
    Zero means no usable PMKID or complete 4-way handshake -- the honest 'nothing to crack'."""
    return sum(1 for ln in hc22000_text.splitlines() if ln.strip().startswith("WPA*"))


def _essid_from_hashline(hashline: str) -> tuple[str, str]:
    """(bssid, ssid) from a 22000 hashline ``WPA*TYPE*hash*MAC_AP*MAC_STA*ESSID_hex*...``. ESSID is
    hex-encoded (field 5); the AP MAC is field 3. Returns ('','') if the shape doesn't match."""
    parts = hashline.split("*")
    if len(parts) < 6 or parts[0] != "WPA":
        return ("", "")
    bssid = parts[3].strip()
    try:
        ssid = bytes.fromhex(parts[5]).decode("utf-8", "replace")
    except ValueError:
        ssid = ""
    return (bssid, ssid)


def parse_hashcat_show(text: str) -> list[dict]:
    """Parse ``hashcat -m 22000 --show`` output into cracked creds.

    Each cracked line is ``<hashline>:<password>``. The 22000 hashline contains no ``:`` (its own
    fields are ``*``-separated), so a split on the FIRST ``:`` cleanly separates it from a password
    that may itself contain colons. Returns [{ssid, bssid, password}, ...] (possibly empty)."""
    creds: list[dict] = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln.startswith("WPA*") or ":" not in ln:
            continue
        hashline, password = ln.split(":", 1)
        bssid, ssid = _essid_from_hashline(hashline)
        creds.append({"ssid": ssid, "bssid": bssid, "password": password})
    return creds


#: aircrack-ng success banner: ``KEY FOUND! [ mypassword ]`` (exactly one space padding inside the
#: brackets). The inner padding is a LITERAL single space and the capture is GREEDY so a passphrase
#: that itself contains ``]`` or a leading/trailing space survives verbatim — a non-greedy ``.*?`` with
#: ``\s*`` padding truncated ``pa]s w0rd`` to ``pa`` and stripped genuine edge spaces (wrong-key bug).
#: ANCHORED to a standalone line (``^[ \t]*`` / ``[ \t]*$``, re.MULTILINE): aircrack echoes the
#: target ESSID verbatim in its selection table, and an ESSID (<=32 bytes, attacker-chosen) can
#: embed the literal ``KEY FOUND! [ x ]``. Un-anchored, ``.search`` matched that column and faked
#: a "cracked" positive for a network we do NOT hold the key to (a verify-never-fake violation). The
#: real banner is alone on its line; the ESSID sits between the BSSID/index columns and a trailing
#: "WPA (n handshake)", so the anchors reject the table row but keep the banner. ``.`` never crosses
#: newlines (no DOTALL), so the greedy capture stays on the banner line.
_AIRCRACK_KEY_RE = re.compile(r"^[ \t]*KEY FOUND!\s*\[ (?P<key>.*) \][ \t]*$", re.MULTILINE)


def parse_aircrack_output(text: str) -> Optional[str]:
    """Recovered passphrase from aircrack-ng output, or None if it reported no key. The password is
    taken verbatim from inside ``KEY FOUND! [ ... ]`` (only the banner's single-space padding is trimmed;
    an internal ``]`` or an edge space in the passphrase is preserved)."""
    m = _AIRCRACK_KEY_RE.search(text or "")
    return m.group("key") if m else None


# -- consent copy (pure) ----------------------------------------------

def capability_text() -> str:
    """Honest one-paragraph description of what this feature is and is not (UI info panel)."""
    return (
        "Offline Wi-Fi key recovery (dictionary attack). Takes a Wi-Fi capture you made "
        "(PMKID or a full WPA/WPA2 4-way handshake) and tries the passphrases in a wordlist you "
        "provide against it. It is dictionary-only: it can only recover a passphrase actually in your "
        "wordlist, and it does not brute-force. CC has a BUILT-IN native cracker (no install, works "
        "out of the box); if you have hashcat or aircrack-ng they're offered as faster optional engines."
    )


def consent_prompt_text(ssid: str = "", bssid: str = "") -> str:
    """Per-run authorization affirmation shown before a crack launches. Names the target if set."""
    target = ssid or bssid or "this network"
    return (
        "AUTHORIZED USE ONLY\n"
        "\n"
        f"You are about to attempt offline passphrase recovery against {target}.\n"
        "\n"
        "Recovering the key to a Wi-Fi network you do not own or are not explicitly authorized (in "
        "writing) to test is illegal in most jurisdictions. This is a dictionary attack: it will "
        "only succeed if the passphrase is in the wordlist you chose.\n"
        "\n"
        "By continuing you confirm you own this network or have written authorization to test it, "
        "and that you accept sole responsibility for this operation.\n"
        "\n"
        "Proceed with the dictionary attack?"
    )


def missing_tools_text(backend: str, tools: dict[str, ToolStatus]) -> str:
    """Honest 'what to install' message when the requested *backend* isn't fully available."""
    if backend == "hashcat":
        need = [n for n in (CONVERTER, HASHCAT) if not tools.get(n, ToolStatus(n)).present]
    elif backend == "aircrack":
        need = [AIRCRACK] if not tools.get(AIRCRACK, ToolStatus(AIRCRACK)).present else []
    else:
        need = []
    if not need:
        return ""
    joined = " and ".join(need)
    is_are = "is" if len(need) == 1 else "are"
    return (
        f"The {backend} crack path needs {joined}, which {is_are} not installed / not on PATH. "
        "Use 'Get tools' to fetch aircrack-ng (a complete backend, no converter needed), or see the "
        "install guidance there for hashcat / hcxtools, then try again."
    )


# -- subprocess orchestration (thin, best-effort) ---------------------

def convert_capture(capture: str, out_hc22000: str, on_line: Line,
                    tools: Optional[dict[str, ToolStatus]] = None,
                    on_proc: Optional[ProcSink] = None) -> int:
    """Run hcxpcapngtool: *capture* -> *out_hc22000*. Returns the extractable-hash count (0 =
    nothing usable). Raises ValueError on bad input / missing converter; RuntimeError if the tool
    timed out. *on_proc* receives the spawned child so a UI's Stop can kill it (the convert step can be
    slow on a large capture; without this, Stop was a no-op until the converter finished)."""
    validate_capture(capture)
    tools = tools or detect_tools()
    conv = tools.get(CONVERTER, ToolStatus(CONVERTER))
    if not conv.present:
        raise ValueError(missing_tools_text("hashcat", tools))
    argv = build_convert_argv(capture, out_hc22000, conv.path or CONVERTER)
    on_line(f"[crack] converting capture: {' '.join(os.path.basename(a) for a in argv)}")
    try:
        rc, out, err = _run_tool(argv, 120, on_proc=on_proc)
    except subprocess.TimeoutExpired:
        raise RuntimeError("hcxpcapngtool timed out converting the capture")
    for ln in (out or "").splitlines():
        if ln.strip():
            on_line(f"[hcx] {ln.strip()}")
    # A NONZERO exit means the converter itself FAILED (corrupt/truncated/unreadable capture,
    # a crash), NOT that the capture holds no handshake. hcxpcapngtool exits 0 even when it
    # extracts zero hashes from a VALID pcap, so rc != 0 is a genuine tool failure. Reporting it
    # as the honest negative "nothing to crack" (as the size==0 branch below does) would launder a
    # real failure into a clean result (verify-never-fake) and bury the cause — its stderr was
    # being dropped too. Surface it with the tool's own stderr, exactly as run_hashcat/run_aircrack
    # now do for their rc != 0 (beat 242).
    if rc != 0:
        for ln in (err or "").splitlines():
            if ln.strip():
                on_line(f"[hcx:err] {ln.strip()}")
        tail = [ln for ln in (err or out or "").splitlines() if ln.strip()]
        hint = tail[-1].strip() if tail else f"exit {rc}"
        raise RuntimeError(f"hcxpcapngtool failed to convert the capture (exit {rc}): {hint}")
    # rc == 0: no output file, a present-but-empty one (the UI pre-creates the temp path, so the
    # absent-file guard can't fire), or zero extractable hashes all mean the SAME honest thing —
    # this capture holds no PMKID/handshake. Return 0 so the caller does NOT feed an empty
    # .hc22000 to hashcat.
    if not os.path.isfile(out_hc22000) or os.path.getsize(out_hc22000) == 0:
        on_line("[crack] no PMKID or handshake found in this capture (nothing to crack)")
        return 0
    with open(out_hc22000, "r", encoding="utf-8", errors="replace") as f:
        n = count_extractable(f.read())
    if n == 0:
        on_line("[crack] no PMKID or handshake found in this capture (nothing to crack)")
        return 0
    on_line(f"[crack] extracted {n} crackable hash(es)")
    return n


def _read_show_results(hash_file: str, wordlist: str, hashcat_path: str,
                       on_line: Line) -> list[dict]:
    """Run `hashcat --show` and parse the potfile-backed cracked list. Best-effort -- a failure to
    read back returns [] (reported as 'not cracked'), never a fabricated hit."""
    argv = build_hashcat_argv(hash_file, wordlist, hashcat_path, show=True)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        on_line(f"[crack] could not read hashcat results: {exc}")
        return []
    return parse_hashcat_show(r.stdout or "")


ProcSink = Callable[["subprocess.Popen"], None]


def _spawn_kwargs() -> dict:
    """Spawn a crack child in its OWN process group so a Stop can kill the whole tree (the tool may fork
    helpers), on Windows and POSIX."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_proc_tree(proc: Optional["subprocess.Popen"]) -> None:
    """Best-effort kill of *proc* and its process group. A UI's Stop calls this so a running aircrack /
    hashcat child is actually terminated instead of orphaned — QThread.terminate() on the wrapper thread
    kills only the Python thread, never the separate OS process it is blocked waiting on."""
    if proc is None or proc.poll() is not None:
        return
    if os.name != "nt":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()   # TerminateProcess on Windows / SIGKILL the direct child on POSIX
    except OSError:
        pass


def _run_tool(argv: list[str], timeout: Optional[float],
              on_proc: Optional[ProcSink] = None) -> tuple[int, str, str]:
    """Run *argv* to completion and return ``(returncode, stdout, stderr)``. The child runs in its own
    process group and is handed to *on_proc* the instant it spawns, so a caller can kill it (and any
    children) on Stop. Raises :class:`subprocess.TimeoutExpired` (after killing the group) on timeout."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            **_spawn_kwargs())
    if on_proc is not None:
        on_proc(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_proc_tree(proc)
        proc.communicate()   # reap the killed child so no handle/zombie leaks
        raise
    return proc.returncode, out or "", err or ""


def run_hashcat(hash_file: str, wordlist: str, on_line: Line,
                tools: Optional[dict[str, ToolStatus]] = None,
                timeout: Optional[float] = None,
                on_proc: Optional[ProcSink] = None) -> CrackResult:
    """Run a hashcat mode-22000 dictionary attack, then read the result back via ``--show``.
    Returns a :class:`CrackResult`. Raises ValueError on bad input / missing tool. *on_proc* receives the
    spawned child so a UI's Stop can kill it (see :func:`kill_proc_tree`)."""
    validate_wordlist(wordlist)
    tools = tools or detect_tools()
    hc = tools.get(HASHCAT, ToolStatus(HASHCAT))
    if not hc.present:
        raise ValueError(missing_tools_text("hashcat", tools))

    argv = build_hashcat_argv(hash_file, wordlist, hc.path or HASHCAT)
    on_line(f"[crack] hashcat -m {HASHCAT_MODE_WPA} -a 0 (dictionary) started")
    rc: Optional[int] = None
    stderr_txt = ""
    timed_out = False
    try:
        rc, out, err = _run_tool(argv, timeout, on_proc=on_proc)
        stderr_txt = err.strip()
        for ln in out.splitlines():
            low = ln.lower()
            if any(k in low for k in ("recovered", "exhausted", "status", "cracked")):
                on_line(f"[hashcat] {ln.strip()}")
    except subprocess.TimeoutExpired:
        on_line("[crack] hashcat timed out (partial wordlist tried)")
        timed_out = True

    creds = _read_show_results(hash_file, wordlist, hc.path or HASHCAT, on_line)
    if creds:
        first = creds[0]
        res = CrackResult(cracked=True, ssid=first["ssid"], bssid=first["bssid"],
                          password=first["password"], detail="key recovered", extra=creds[1:])
        on_line(f"[crack] KEY RECOVERED for {res.ssid or res.bssid}: {res.password}")
        return res
    # No key recovered. hashcat exit codes: 0 = cracked, 1 = exhausted (both legitimate). ANY other exit
    # is a TOOL FAILURE that tested nothing (no OpenCL/CUDA device, a malformed .hc22000, etc.). Reporting
    # that as "key not in wordlist" is a false negative — the engine's verify-never-fake contract requires
    # surfacing the real error and its stderr instead of a misleading clean negative.
    if rc is not None and rc not in (0, 1):
        detail = f"hashcat failed (exit {rc}) — the wordlist was NOT tested; the negative is not trustworthy"
        last = stderr_txt.splitlines()[-1].strip() if stderr_txt else ""
        if last:
            on_line(f"[hashcat] {last}")
            detail += f": {last}"
        on_line(f"[crack] {detail}")
        return CrackResult(cracked=False, detail=detail)
    if timed_out:
        # A timeout killed hashcat before it finished the wordlist — the dictionary was NOT exhausted.
        # Claiming "dictionary exhausted" here would be a fabricated honest-negative (the wordlist may
        # still hold the key), contradicting the "partial wordlist tried" line already logged. Mirror
        # run_aircrack's honest timeout negative.
        return CrackResult(cracked=False, detail="timed out before exhausting the wordlist")
    if rc == 0:
        # hashcat exited 0 = it CRACKED the hash, yet the --show read-back above returned no creds
        # (a potfile path/permission race, or --show couldn't re-derive the line). Reporting
        # "dictionary exhausted" here directly contradicts hashcat's own success exit — a false
        # negative. Surface the discrepancy instead of a fabricated clean negative.
        detail = ("hashcat reported a crack (exit 0) but the recovered key could not be read back "
                  "via --show — rerun or check the hashcat potfile; "
                  "the negative is not trustworthy")
        on_line(f"[crack] {detail}")
        return CrackResult(cracked=False, detail=detail)
    return CrackResult(cracked=False, detail="key not in wordlist (dictionary exhausted)")


def run_aircrack(capture: str, wordlist: str, on_line: Line,
                 tools: Optional[dict[str, ToolStatus]] = None,
                 bssid: str = "", timeout: Optional[float] = None,
                 on_proc: Optional[ProcSink] = None) -> CrackResult:
    """Run an aircrack-ng dictionary attack directly on the *capture* (CPU fallback path). *on_proc*
    receives the spawned child so a UI's Stop can kill it (see :func:`kill_proc_tree`)."""
    validate_capture(capture)
    validate_wordlist(wordlist)
    tools = tools or detect_tools()
    ac = tools.get(AIRCRACK, ToolStatus(AIRCRACK))
    if not ac.present:
        raise ValueError(missing_tools_text("aircrack", tools))

    argv = build_aircrack_argv(capture, wordlist, ac.path or AIRCRACK, bssid=bssid)
    on_line("[crack] aircrack-ng dictionary attack started")
    try:
        rc, out, err = _run_tool(argv, timeout, on_proc=on_proc)
    except subprocess.TimeoutExpired:
        on_line("[crack] aircrack-ng timed out (partial wordlist tried)")
        return CrackResult(cracked=False, detail="timed out before exhausting the wordlist")

    key = parse_aircrack_output(out)
    if key is not None:
        on_line(f"[crack] KEY RECOVERED: {key}")
        return CrackResult(cracked=True, password=key, bssid=bssid, detail="key recovered")

    # No key parsed. Distinguish a GENUINE exhaustion (aircrack ran the wordlist against a valid
    # handshake — it prints a live "N/M keys tested" progress line) from a BAIL-OUT where it tested
    # nothing (no valid WPA handshake in the capture, wrong BSSID, or a tool error). Reporting the
    # latter as "dictionary exhausted" would be a fabricated honest-negative — the wordlist was NOT
    # tried, so the negative isn't trustworthy. Surface the real problem (mirrors run_hashcat).
    if "keys tested" in out.lower():
        return CrackResult(cracked=False, detail="key not in wordlist (dictionary exhausted)")
    detail = ("aircrack-ng tested no keys — the capture likely holds no valid WPA handshake for "
              "this target; the wordlist was NOT tried, so this is not a real exhaustion")
    if rc:
        detail += f" (aircrack-ng exit {rc})"
    last = err.strip().splitlines()[-1].strip() if err.strip() else ""
    if last:
        on_line(f"[aircrack] {last}")
        detail += f": {last}"
    on_line(f"[crack] {detail}")
    return CrackResult(cracked=False, detail=detail)
