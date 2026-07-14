r"""Beat 269 - serial-parser ReDoS in flipper/marauder/bw16 (cc-deep-audit-11 [3] MED).

Several per-line parser regexes used the shape `(.+?)\s*<delim>` / `(.*?)\s*<delim>` where the lazy
dot and the following `\s*`/`\s+` overlap on whitespace and the terminating delimiter may be absent
from the line. On a long line lacking the delimiter the engine backtracks the whitespace run against
every lazy-dot expansion -> O(n^2) in the line length. serial_handler._reader_loop flushes an
un-terminated read buffer as ONE line once it passes _MAX_LINE_CHARS = 64*1024, so a device that
streams ~64 KiB with no CR/LF -- a matching prefix, a long INTERNAL whitespace run, then a non-space
(so flipper's parse_line .strip() doesn't remove it; marauder/bw16 don't strip) -- drives the regex
through billions of steps and pins that port's reader thread for ~20s (flipper) / ~10s (marauder) /
~5s (bw16): a sustained DoS of that port's ingestion. The vulnerable captures:
  flipper  _RE_NFC / _RE_NFC_FULL / _RE_BT   `(.+?)\s*\|`
  marauder _RE_AP                            `(.+?)\s+BSSID:`
  bw16     _RE_AP_VAMPIRE / _RE_AP_BRACKET   `(.*?)\s*\(` / `(.+?)`

Fix: bound each lazy capture to <=64 chars (an SSID is <=32 octets; NFC types / BT names are short),
converting the O(n^2) backtrack to O(n). A 64 KiB pathological line now parses in ~0.03ms.

Discriminating (unbounded-compute class -> a hang can't be run in-process): a KILLABLE subprocess
feeds each parser a ~100 KiB pathological line. The fix returns in ms (exit 0, DONE); on HEAD it
backtracks for seconds and `subprocess.run(timeout=...)` raises TimeoutExpired -> the test fails.
The guard confirms every legit line still parses correctly.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest


def _repo_root() -> str:
    return str(pathlib.Path(__file__).resolve().parents[1])


# (module, class, matching prefix that starts the regex but never reaches the delimiter)
_CASES = [
    ("flipper", "FlipperProtocol", "NFC: Type: A"),
    ("marauder", "MarauderProtocol", "AP: A"),
    ("bw16", "BW16Protocol", "0: A"),
]

# The long whitespace run must be INTERNAL and the line must END in a non-space: FlipperProtocol.
# parse_line strips the line first, so a purely-trailing-space run would be removed before the regex
# (marauder/bw16 don't strip, but a trailing 'Z' keeps all three faithful to one reachable line).
_SNIPPET = (
    "import sys; sys.path.insert(0, r'{root}')\n"
    "from src.protocols.{mod} import {cls}\n"
    "bad = '{prefix}' + ' ' * 100000 + 'Z'\n"   # ~100 KiB: prefix, internal spaces, no delim, 'Z'
    "{cls}().parse_line(bad)\n"
    "print('DONE')\n"
)


@pytest.mark.parametrize("mod,cls,prefix", _CASES)
def test_parser_redos_bounded_killable_subprocess(mod, cls, prefix):
    """Discriminating: a ~100 KiB pathological line parses fast (fix) instead of hanging (HEAD)."""
    root = _repo_root()
    code = _SNIPPET.format(root=root, mod=mod, cls=cls, prefix=prefix)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=root,
            capture_output=True, text=True, timeout=6,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(
            f"{mod}.parse_line did not terminate on a ~100 KiB pathological line within 6s "
            f"(HEAD: O(n^2) regex backtracking -> serial-reader thread DoS)",
        )
    assert proc.returncode == 0, f"child failed: {proc.stderr}"
    assert "DONE" in proc.stdout


def test_legit_lines_still_parse():
    """Guard (passes on HEAD + fix): the bounded captures still parse every normal device line."""
    from src.protocols.bw16 import BW16Protocol
    from src.protocols.flipper import FlipperProtocol
    from src.protocols.marauder import MarauderProtocol

    nfc = FlipperProtocol().parse_line(
        "NFC: Type: Mifare Classic 1K | UID: 04:AB:CD:EF | ATQA: 0004 | SAK: 08")
    assert nfc is not None and nfc.data["nfc_type"] == "Mifare Classic 1K"
    assert nfc.data["sak"] == "08"

    bt = FlipperProtocol().parse_line("BT: Name: MyDevice | MAC: AA:BB:CC:DD:EE:FF | RSSI: -55")
    assert bt is not None and bt.data["name"] == "MyDevice"

    ap = MarauderProtocol().parse_line("AP: MyNet BSSID: aa:bb:cc:dd:ee:ff Ch: 6 RSSI: -42")
    assert ap is not None and ap.data["ssid"] == "MyNet" and ap.data["channel"] == 6

    vamp = BW16Protocol().parse_line("0: KashPatels007 (CH 1, RSSI -42)")
    assert vamp is not None and vamp.data["ssid"] == "KashPatels007" and vamp.data["channel"] == 1

    brack = BW16Protocol().parse_line("[0] MySSID  ch:6  -42dBm  AA:BB:CC:DD:EE:FF")
    assert brack is not None and brack.data["ssid"] == "MySSID"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
