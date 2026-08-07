"""Host-shell bridge — a terminal onto the machine Cyber Controller itself runs on.

Owner callout: "the terminal should interact with the actual device's terminal as well, like the device
the software is running off of." The existing TERMINAL surface talks to *serial* devices; this adds a
console onto the HOST.

This is remote code execution by nature, so it is DISABLED by default and wrapped in a strict envelope the
web layer enforces before ever constructing a session (see :func:`host_shell_availability`):

    * OFF unless the operator explicitly opts in with ``CC_WEB_HOST_SHELL=1``;
    * REFUSED whenever ``CC_WEB_ALLOW_LAN=1`` — a host shell must never be reachable from a LAN, even
      deliberately; if the web server is in its LAN-exposed mode the host shell stays off, full stop;
    * loopback-only (the server must be bound to 127.0.0.1/localhost);
    * spawned as the CURRENT user with no privilege elevation.

It is a PIPE-based bridge, not a full PTY/ConPTY: line-oriented commands (dir/ls/ipconfig/git/python …)
work; full-screen TUIs need a real pseudo-terminal, which is a later upgrade (pywinpty on Windows / pty on
POSIX). :meth:`HostShellSession.resize` is a deliberate no-op kept so that upgrade is drop-in. Nothing here
touches or bypasses the serial safety gates — it is a separate, opt-in host console with its own gate.
"""
from __future__ import annotations

import os
import subprocess
import threading
from typing import Callable, Optional

# --- environment knobs (all read by the web layer, never by user input) ------------------------------
_CONSENT_ENV = "CC_WEB_HOST_SHELL"   # must be exactly "1" to enable the host shell at all (default OFF)
_ALLOW_LAN_ENV = "CC_WEB_ALLOW_LAN"  # if "1", the host shell is REFUSED (never LAN-reachable)
_SHELL_CMD_ENV = "CC_HOST_SHELL_CMD"  # optional absolute path to override the spawned shell

OutputCb = Callable[[str], None]


def default_shell() -> list[str]:
    """The shell to spawn: an explicit ``CC_HOST_SHELL_CMD`` override, else the platform default
    (``%COMSPEC%``/cmd.exe on Windows, ``$SHELL``/`/bin/sh` on POSIX). No arguments — commands arrive on
    stdin, one line at a time."""
    override = os.environ.get(_SHELL_CMD_ENV)
    if override:
        return [override]
    if os.name == "nt":
        return [os.environ.get("COMSPEC", "cmd.exe")]
    return [os.environ.get("SHELL", "/bin/sh")]


def host_shell_availability(*, is_loopback: bool, allow_lan: bool, consent: bool) -> tuple[bool, str]:
    """Fail-closed gate — the single source of truth for whether a host shell may run.

    Returns ``(enabled, human_reason)``. It stays OFF unless ALL hold: the operator consented, the server
    is not LAN-exposed, and the bind is loopback. The checks are ordered so the reason names the most
    actionable blocker first. Pure (no globals/IO) so every branch is directly unit-testable."""
    if not consent:
        return False, "host shell is disabled — set CC_WEB_HOST_SHELL=1 to enable it (loopback only)"
    if allow_lan:
        return False, "host shell refuses to run while CC_WEB_ALLOW_LAN=1 — it must never be reachable from a LAN"
    if not is_loopback:
        return False, "host shell is loopback-only (the server is not bound to 127.0.0.1/localhost)"
    return True, "enabled"


def availability_from_env(*, is_loopback: bool) -> tuple[bool, str]:
    """:func:`host_shell_availability` with the two policy flags read from the environment."""
    return host_shell_availability(
        is_loopback=is_loopback,
        allow_lan=os.environ.get(_ALLOW_LAN_ENV) == "1",
        consent=os.environ.get(_CONSENT_ENV) == "1",
    )


class HostShellSession:
    """A single spawned host shell, streaming its combined stdout+stderr through ``on_output``.

    Construct ONLY after :func:`host_shell_availability` says enabled — the class itself does not re-check
    the envelope (the web layer owns that boundary), it just runs a subprocess as the current user."""

    def __init__(self, on_output: OutputCb, *, shell_cmd: Optional[list[str]] = None,
                 cwd: Optional[str] = None) -> None:
        self._on_output = on_output
        self._cmd = shell_cmd or default_shell()
        self._cwd = cwd or os.path.expanduser("~")
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        """Spawn the shell (idempotent) and begin pumping its output. No elevation: it inherits exactly the
        privileges of the process CC runs as."""
        with self._lock:
            if self.is_alive:
                return
            self._proc = subprocess.Popen(  # noqa: S603 — intentional host shell, gated by the web layer
                self._cmd,
                cwd=self._cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,   # merge stderr into stdout so it reads like one terminal
                bufsize=0,                  # unbuffered -> raw streaming, not line-buffered lumps
                close_fds=True,
            )
            self._reader = threading.Thread(target=self._pump, name="host-shell-reader", daemon=True)
            self._reader.start()

    def _pump(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                self._safe_emit(chunk.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 — the reader thread must never take down the process
            pass
        finally:
            code = proc.poll()
            tail = "" if code is None else f" (code {code})"
            self._safe_emit(f"\r\n[host shell exited{tail}]\r\n")

    def _safe_emit(self, text: str) -> None:
        try:
            self._on_output(text)
        except Exception:  # noqa: BLE001 — a broken sink must not kill the reader
            pass

    def write(self, data: str) -> None:
        """Write raw bytes to the shell's stdin (no automatic newline)."""
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None or proc.stdin is None:
                return
            try:
                proc.stdin.write(data.encode("utf-8", "replace"))
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def send_line(self, line: str) -> None:
        """Submit one command line (newline appended for the platform)."""
        self.write(line.rstrip("\r\n") + os.linesep)

    def resize(self, cols: int, rows: int) -> None:  # noqa: ARG002 — API stub
        """No-op without a real PTY. Kept so a future pywinpty/pty upgrade is drop-in."""
        return

    def kill(self) -> None:
        """Terminate the shell (idempotent). SIGTERM first, then a hard kill if it lingers."""
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass
