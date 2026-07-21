r"""Run a bundled / detected security tool as a local subprocess — powers the terminal "tool shell".

The owner wants full command-line access to the tools, not just the Crack Lab buttons: type e.g.
``aircrack-ng -w list.txt cap.cap`` or ``hashcat -m 22000 hash hccapx wl.txt`` in the bottom terminal
and CC runs that tool with those args, streaming its output into the activity console.

Deliberately scoped to the KNOWN tools (the aircrack-ng suite + hashcat + hcxpcapngtool) — this is NOT
a general OS shell, so a typo or a hostile paste can't launch an arbitrary program; anything whose first
word isn't a known tool falls through to the normal serial-terminal path. Resolves the binary from the
bundled/enabled tools folder first, then PATH.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from typing import Callable, Optional

Line = Callable[[str], None]
Exit = Callable[[int], None]

#: The tools the terminal will run locally. Aircrack suite + the crack backends. Anything else -> serial.
KNOWN_TOOLS: frozenset[str] = frozenset({
    "aircrack-ng", "airodump-ng", "aireplay-ng", "airbase-ng", "airdecap-ng", "airdecloak-ng",
    "airolib-ng", "airserv-ng", "airtun-ng", "airventriloquist-ng", "besside-ng", "buddy-ng",
    "easside-ng", "ivstools", "kstats", "makeivs-ng", "packetforge-ng", "tkiptun-ng", "wesside-ng",
    "wpaclean", "hashcat", "hcxpcapngtool",
})


def _base(name: str) -> str:
    n = os.path.basename(name).lower()
    return n[:-4] if n.endswith(".exe") else n


def is_tool_command(first_token: str) -> bool:
    """True if *first_token* names one of the known local tools (so the terminal should run it, not
    send it to a serial device)."""
    return _base(first_token) in KNOWN_TOOLS


#: Valid send-target selector values for the persistent terminal input.
SEND_TARGETS: tuple[str, ...] = ("auto", "serial", "computer")


def route_terminal_send(target: str, first_token: str) -> str:
    """Decide where a typed terminal line goes, given the explicit send-target selector.

    The owner wants to choose where a line is sent (computer shell vs the connected serial device),
    not have it inferred silently. This is the pure decision function behind that selector:

    - ``"auto"``     — route by content: a known local tool (aircrack-ng/hashcat/…) runs on the
      computer, everything else is written to the serial device(s). (The original behaviour.)
    - ``"serial"``   — force the connected device(s), even when the first word looks like a tool
      name (so a firmware command that happens to share a tool's name still reaches the board).
    - ``"computer"`` — force the local tool shell; a first word that isn't a known tool is refused
      rather than leaking to a device, because this is a scoped tool runner, not a general OS shell.

    Returns one of ``"tool"`` (run locally), ``"serial"`` (write to devices), or ``"no-tool"`` (the
    computer target was chosen but *first_token* isn't a known tool). An unknown *target* is treated
    as ``"auto"``.
    """
    t = (target or "auto").strip().lower()
    if t == "serial":
        return "serial"
    if t == "computer":
        return "tool" if is_tool_command(first_token) else "no-tool"
    # "auto" (and any unknown target): known tool runs locally, everything else goes to the device.
    return "tool" if is_tool_command(first_token) else "serial"


def resolve_tool(name: str) -> Optional[str]:
    """Absolute path to *name*'s executable: the bundled/enabled tools folder first (one level deep —
    aircrack's suite installs into ``tools/aircrack-ng/``), then PATH. None if not available."""
    from .tool_bundle import enable_dir
    base = _base(name)
    wanted = (base, base + ".exe")
    root = enable_dir()
    if os.path.isdir(root):
        for entry in sorted(os.listdir(root)):
            d = os.path.join(root, entry)
            if os.path.isdir(d):
                for w in wanted:
                    p = os.path.join(d, w)
                    if os.path.isfile(p):
                        return p
            elif entry.lower() in wanted:
                return d
    return shutil.which(base)


def run_tool(argv: list[str], on_line: Line, on_exit: Exit,
             cwd: Optional[str] = None) -> Optional[subprocess.Popen]:
    """Spawn ``argv`` (a known tool + its args), streaming stdout/stderr line-by-line to *on_line* and
    calling *on_exit(returncode)* when it finishes. Returns the Popen (so a caller can kill it) or None
    if the tool isn't available. Runs in *cwd* (default the user's home) so relative capture/wordlist
    paths resolve predictably. Non-blocking: a reader thread pumps the output."""
    if not argv:
        return None
    exe = resolve_tool(argv[0])
    if exe is None:
        on_line(f"'{_base(argv[0])}' isn't available — enable it in Crack Lab ▸ Get tools, or install it.")
        on_exit(127)
        return None
    real = [exe] + list(argv[1:])
    try:
        proc = subprocess.Popen(
            real, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd or os.path.expanduser("~"),
            text=True, bufsize=1, errors="replace")
    except OSError as exc:
        on_line(f"could not start {_base(argv[0])}: {exc}")
        on_exit(126)
        return None

    def _pump() -> None:
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                on_line(raw.rstrip("\r\n"))
        except Exception:  # noqa: BLE001 — a read hiccup must not crash the reader thread
            pass
        finally:
            on_exit(proc.wait())

    threading.Thread(target=_pump, daemon=True).start()
    return proc
