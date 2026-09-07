"""Platform tag classification in build.py.

The script is loaded by file path so a PyInstaller ``build/`` output directory beside it can never
shadow the module. Only ``platform.system`` and ``platform.machine`` are replaced; nothing builds.
"""
from __future__ import annotations

import importlib.util
import platform
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def build_script():
    spec = importlib.util.spec_from_file_location("cc_build_script", ROOT / "build.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CASES = [
    ("Windows", "AMD64", "windows-x64"),
    ("Windows", "x86_64", "windows-x64"),
    ("Windows", "x86", "windows-x86"),
    ("Windows", "ARM64", "windows-arm64"),
    ("Windows", "aarch64", "windows-arm64"),
    ("Windows", "ARM", "windows-arm"),
    ("Linux", "x86_64", "linux-x64"),
    ("Linux", "aarch64", "linux-arm64"),
    ("Linux", "arm64", "linux-arm64"),
    ("Linux", "armv7l", "linux-arm"),
    ("Darwin", "arm64", "macos-arm64"),
    ("Darwin", "x86_64", "macos-x64"),
    ("FreeBSD", "amd64", "freebsd-amd64"),
]


@pytest.mark.parametrize("system,machine,expected", CASES)
def test_platform_tag(build_script, monkeypatch, system, machine, expected):
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    assert build_script._detect_platform() == expected
