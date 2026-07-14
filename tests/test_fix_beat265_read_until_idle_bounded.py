"""Beat 265 -- device_detect._read_until_idle unbounded read (cc-deep-audit-10 [4] MED).

`_read_until_idle` read from a serial port until it saw no new bytes for `timeout` seconds, but it
reset that idle deadline on EVERY read that returned data and never bounded the total time or the
buffer size. A device that streams forever (wedged boot-spew, a chatty console/GPS, a hostile banner
that never goes idle) therefore looped forever and grew `buf` without limit -> the caller hangs and
memory is exhausted. Port enumeration probes ports serially (probe_firmware -> scan_ports /
generate_manifest), so one such port wedges the whole scan. The sibling reader in serial_handler
already bounds itself with `_MAX_LINE_CHARS`; this path did not.

Fix: keep the idle-exit semantics but add two HARD caps that end the read regardless -- an overall
wall-clock deadline (`max_total`, independent of the per-read reset) and a `max_bytes` buffer cap.

Discriminating (unbounded-read class -> a hang can't be run in-process): a KILLABLE subprocess
calls `_read_until_idle` against a serial stub that always has data waiting. On the fix it returns
via the byte cap in milliseconds (exit 0, prints DONE); on HEAD it never returns and
`subprocess.run(timeout=...)` raises TimeoutExpired -> the test fails. The in-suite bounded test
additionally proves DETERMINISTICALLY (by counting reads) that the byte cap terminates the loop
without relying on wall-clock timing, and the idle guard proves a normal device still returns its
banner.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

from src.core import device_detect


def _repo_root() -> str:
    # tests/ lives directly under the repo root.
    return str(pathlib.Path(__file__).resolve().parents[1])


class _EndlessSer:
    """A serial stub that ALWAYS has data waiting -- it never goes idle."""

    def __init__(self, chunk: int = 4096) -> None:
        self._chunk = chunk
        self.reads = 0

    @property
    def in_waiting(self) -> int:
        return self._chunk

    def read(self, n: int) -> bytes:
        self.reads += 1
        return b"\x00" * n


class _IdleAfterChunks:
    """Yields the given chunks once, then reports idle (in_waiting == 0) forever."""

    def __init__(self, chunks) -> None:
        self._chunks = list(chunks)

    @property
    def in_waiting(self) -> int:
        return len(self._chunks[0]) if self._chunks else 0

    def read(self, n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


def test_endless_stream_is_bounded_by_byte_cap():
    """Deterministic: an always-streaming device terminates via the byte cap, not the wall clock.

    Data is always waiting so the loop never sleeps -- it is pure CPU and finishes instantly. The
    read count proves the byte cap (not timing) ended it, and that `buf` stayed bounded near
    max_bytes. On HEAD this call would never return."""
    ser = _EndlessSer(chunk=4096)
    out = device_detect._read_until_idle(ser, timeout=1.5, max_total=10.0, max_bytes=1 << 20)

    assert len(out) <= (1 << 20) + 4096, "buffer must stay bounded near max_bytes"
    assert ser.reads <= (1 << 20) // 4096 + 2, "byte cap, not the 10s wall clock, ended the read"


def test_idle_device_returns_collected_banner():
    """No-regression: a device that emits a banner then goes idle still returns it (HEAD + fix)."""
    ser = _IdleAfterChunks([b"Marauder v1.13.0\n"])
    out = device_detect._read_until_idle(ser, timeout=0.2)

    assert "Marauder v1.13.0" in out


_SNIPPET = (
    "import sys; sys.path.insert(0, r'{root}')\n"
    "from src.core import device_detect\n"
    "class S:\n"
    "    @property\n"
    "    def in_waiting(self):\n"
    "        return 256\n"
    "    def read(self, n):\n"
    "        return b'\\x00' * n\n"
    "device_detect._read_until_idle(S(), 1.5)\n"
    "print('DONE')\n"
)


def test_endless_stream_terminates_killable_subprocess():
    """Discriminating: run the read against an endless stream in a killable child process.

    Fix -> the byte cap ends it in milliseconds (exit 0, 'DONE'). HEAD -> it never returns and the
    8s timeout kills the child, raising TimeoutExpired here, which fails the test. Positional
    (ser, timeout) call works on both the HEAD and fixed signatures. The hang is isolated in the
    child, so pytest itself never hangs."""
    root = _repo_root()
    code = _SNIPPET.format(root=root)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=root,
            capture_output=True, text=True, timeout=8,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(
            "_read_until_idle did not terminate on an endless stream within 8s "
            "(HEAD: no wall-clock/byte cap -> infinite loop)",
        )
    assert proc.returncode == 0, f"child failed: {proc.stderr}"
    assert "DONE" in proc.stdout


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
