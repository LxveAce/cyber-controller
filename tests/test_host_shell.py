"""Tests for the host-shell bridge (src/core/host_shell.py).

Covers the fail-closed security envelope (disabled by default, refuse on LAN, loopback-only) and a real
round-trip: spawn the platform shell, echo a unique token, see it stream back, and kill cleanly.
"""
from __future__ import annotations

import os
import threading
import time

from src.core.host_shell import (
    HostShellSession,
    availability_from_env,
    default_shell,
    host_shell_availability,
)


# ── the envelope: fail-closed ────────────────────────────────────────────────
def test_disabled_without_consent():
    ok, reason = host_shell_availability(is_loopback=True, allow_lan=False, consent=False)
    assert ok is False
    assert "CC_WEB_HOST_SHELL=1" in reason


def test_refused_when_lan_exposed_even_with_consent():
    # A host shell must NEVER be reachable from a LAN — allow_lan wins over consent + loopback.
    ok, reason = host_shell_availability(is_loopback=True, allow_lan=True, consent=True)
    assert ok is False
    assert "LAN" in reason


def test_refused_when_not_loopback():
    ok, reason = host_shell_availability(is_loopback=False, allow_lan=False, consent=True)
    assert ok is False
    assert "loopback" in reason.lower()


def test_enabled_only_when_consent_and_loopback_and_not_lan():
    ok, reason = host_shell_availability(is_loopback=True, allow_lan=False, consent=True)
    assert ok is True
    assert reason == "enabled"


def test_availability_from_env(monkeypatch):
    monkeypatch.delenv("CC_WEB_HOST_SHELL", raising=False)
    monkeypatch.delenv("CC_WEB_ALLOW_LAN", raising=False)
    assert availability_from_env(is_loopback=True)[0] is False        # default OFF

    monkeypatch.setenv("CC_WEB_HOST_SHELL", "1")
    assert availability_from_env(is_loopback=True)[0] is True         # opted in, loopback

    monkeypatch.setenv("CC_WEB_ALLOW_LAN", "1")
    assert availability_from_env(is_loopback=True)[0] is False        # LAN exposure refuses it

    monkeypatch.setenv("CC_WEB_ALLOW_LAN", "0")
    assert availability_from_env(is_loopback=False)[0] is False       # non-loopback refuses it


def test_default_shell_is_platform_appropriate(monkeypatch):
    monkeypatch.delenv("CC_HOST_SHELL_CMD", raising=False)
    cmd = default_shell()
    assert isinstance(cmd, list) and cmd and isinstance(cmd[0], str)
    if os.name == "nt":
        assert cmd[0].lower().endswith("cmd.exe") or "COMSPEC" not in os.environ
    else:
        assert cmd[0].startswith("/")


def test_shell_cmd_override(monkeypatch):
    monkeypatch.setenv("CC_HOST_SHELL_CMD", "/opt/custom/sh")
    assert default_shell() == ["/opt/custom/sh"]


# ── a real round-trip ────────────────────────────────────────────────────────
class _Collector:
    def __init__(self):
        self.buf = []
        self.got = threading.Event()
        self._token = None

    def watch(self, token):
        self._token = token

    def __call__(self, text):
        self.buf.append(text)
        if self._token and self._token in "".join(self.buf):
            self.got.set()

    @property
    def text(self):
        return "".join(self.buf)


def test_echo_roundtrip_and_kill():
    token = "CC_HOSTSHELL_OK_9F3A"
    sink = _Collector()
    sink.watch(token)
    sess = HostShellSession(sink)
    sess.start()
    try:
        assert sess.is_alive
        sess.send_line(f"echo {token}")
        # give the shell time to spawn (banner) + run the echo
        assert sink.got.wait(timeout=12), f"token not seen; output so far: {sink.text!r}"
        assert token in sink.text
    finally:
        sess.kill()
    # kill is synchronous enough that the process is gone shortly after
    for _ in range(30):
        if not sess.is_alive:
            break
        time.sleep(0.1)
    assert not sess.is_alive


def test_start_is_idempotent_and_write_after_kill_is_safe():
    sess = HostShellSession(lambda _t: None)
    sess.start()
    proc1 = sess._proc
    sess.start()  # second start must NOT spawn a second process
    assert sess._proc is proc1
    sess.kill()
    # writing to a dead session must be a quiet no-op, never raise
    sess.write("echo nope\n")
    sess.send_line("echo nope")


def test_exit_notice_emitted_when_shell_ends():
    notices = _Collector()
    sess = HostShellSession(notices)
    sess.start()
    sess.kill()
    # the reader's finally-branch emits an "[host shell exited...]" line
    deadline = time.time() + 5
    while time.time() < deadline and "host shell exited" not in notices.text:
        time.sleep(0.1)
    assert "host shell exited" in notices.text
