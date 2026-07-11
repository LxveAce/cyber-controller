"""Unit tests for the terminal tool-shell runner (src/core/tool_runner.py)."""
from __future__ import annotations

import sys
import threading

from src.core import tool_runner


def test_is_tool_command_scoping():
    assert tool_runner.is_tool_command("aircrack-ng")
    assert tool_runner.is_tool_command("HASHCAT.exe")
    assert tool_runner.is_tool_command("hcxpcapngtool")
    # anything not a known tool falls through to the serial terminal
    assert not tool_runner.is_tool_command("scanap")
    assert not tool_runner.is_tool_command("reboot")
    assert not tool_runner.is_tool_command("rm")


def test_resolve_from_bundle_dir(tmp_path, monkeypatch):
    import src.core.tool_bundle as tb
    (tmp_path / "aircrack-ng").mkdir()
    (tmp_path / "aircrack-ng" / "aircrack-ng.exe").write_bytes(b"x")
    (tmp_path / "aircrack-ng" / "aireplay-ng.exe").write_bytes(b"x")
    monkeypatch.setattr(tb, "enable_dir", lambda: str(tmp_path))
    assert tool_runner.resolve_tool("aircrack-ng").endswith("aircrack-ng.exe")
    assert tool_runner.resolve_tool("aireplay-ng").endswith("aireplay-ng.exe")
    assert tool_runner.resolve_tool("not-a-tool-here") in (None, tool_runner.shutil.which("not-a-tool-here"))


def test_run_tool_streams_and_exits(monkeypatch):
    # Point the resolver at the Python interpreter and run a tiny script -> proves the stream+exit path.
    monkeypatch.setattr(tool_runner, "resolve_tool", lambda _n: sys.executable)
    lines: list[str] = []
    done: list[int] = []
    ev = threading.Event()
    tool_runner.run_tool(["hashcat", "-c", "print('hello-tool')"],
                         on_line=lines.append,
                         on_exit=lambda rc: (done.append(rc), ev.set()))
    assert ev.wait(20), "tool did not finish in time"
    assert done == [0]
    assert any("hello-tool" in ln for ln in lines)


def test_run_tool_missing_is_honest(monkeypatch):
    monkeypatch.setattr(tool_runner, "resolve_tool", lambda _n: None)
    lines: list[str] = []
    done: list[int] = []
    tool_runner.run_tool(["aircrack-ng"], on_line=lines.append, on_exit=done.append)
    assert done == [127]
    assert any("isn't available" in ln for ln in lines)
