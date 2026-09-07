"""PyInstaller command assembly in build.py, driven with a mocked module-discovery function.

The script is loaded by file path so a PyInstaller ``build/`` output directory beside it can never
shadow the module. No PyInstaller, build, install or real optional import happens here: every
optional input is decided through the injected ``available`` callable.
"""
from __future__ import annotations

import importlib.util
import platform
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
QT_WEB = ("PyQt5.QtWebEngineWidgets", "PyQt5.QtWebEngineCore")
MANDATORY_QT = ("PyQt5", "PyQt5.sip", "PyQt5.QtCore", "PyQt5.QtGui", "PyQt5.QtWidgets",
                "PyQt5.QtSvg", "PyQt5.QtWebChannel", "PyQt5.QtNetwork", "PyQt5.QtPrintSupport")


@pytest.fixture(scope="module")
def build_script():
    spec = importlib.util.spec_from_file_location("cc_build_script", ROOT / "build.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def values_after(cmd, flag):
    return [cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg == flag]


def assemble(build_script, monkeypatch, system, *, missing=(), onedir=False):
    monkeypatch.setattr(platform, "system", lambda: system)
    absent = set(missing)
    return build_script._assemble_command(onedir, available=lambda name: name not in absent)


def test_windows_without_qtwebengine_omits_only_the_optional_names(build_script, monkeypatch,
                                                                    capsys):
    cmd = assemble(build_script, monkeypatch, "Windows", missing=QT_WEB)
    hidden = values_after(cmd, "--hidden-import")
    for name in QT_WEB:
        assert name not in hidden
    for name in MANDATORY_QT:
        assert name in hidden
    assert "esptool" in values_after(cmd, "--collect-all")
    assert "qtpy" not in values_after(cmd, "--collect-all")
    assert "Qt desktop shell will not be bundled" in capsys.readouterr().out


def test_windows_with_qtwebengine_requests_the_names(build_script, monkeypatch, capsys):
    cmd = assemble(build_script, monkeypatch, "Windows")
    hidden = values_after(cmd, "--hidden-import")
    for name in QT_WEB:
        assert name in hidden
    assert "will not be bundled" not in capsys.readouterr().out


def test_linux_with_qtwebengine_requests_the_names_and_collects_qtpy(build_script, monkeypatch):
    cmd = assemble(build_script, monkeypatch, "Linux")
    hidden = values_after(cmd, "--hidden-import")
    for name in QT_WEB:
        assert name in hidden
    assert "qtpy" in values_after(cmd, "--collect-all")
    assert all(":" in item for item in values_after(cmd, "--add-data"))


@pytest.mark.parametrize("onedir", [False, True])
def test_mode_and_identity_flags_are_untouched_by_the_gate(build_script, monkeypatch, onedir):
    with_qt = assemble(build_script, monkeypatch, "Windows", onedir=onedir)
    without_qt = assemble(build_script, monkeypatch, "Windows", missing=QT_WEB, onedir=onedir)
    for cmd in (with_qt, without_qt):
        assert ("--onedir" in cmd) is onedir
        assert ("--onefile" in cmd) is not onedir
        assert "--windowed" in cmd
        assert values_after(cmd, "--name") == ["CyberController"]
        assert cmd[-1].endswith("app.py")
    assert [a for a in with_qt if a not in without_qt] == list(QT_WEB)


def test_optional_packages_are_skipped_with_notes_when_unavailable(build_script, monkeypatch,
                                                                    capsys):
    missing = ("pyzipper", "webview", "esp_idf_nvs_partition_gen", "flask")
    cmd = assemble(build_script, monkeypatch, "Windows", missing=missing)
    collected = values_after(cmd, "--collect-all")
    for name in missing:
        assert name not in collected
    assert "flask_socketio" in collected
    out = capsys.readouterr().out
    assert "pyzipper not installed" in out
    assert "pywebview not installed" in out
    assert "esp_idf_nvs_partition_gen not installed" in out
    assert "flask not installed" in out


@pytest.fixture
def probe_tree(tmp_path, monkeypatch):
    """An inert package tree on sys.path whose parent __init__ raises if it is ever executed."""
    parent = tmp_path / "cc_probe_parent"
    parent.mkdir()
    (parent / "__init__.py").write_text("raise RuntimeError('inert parent WAS executed')\n",
                                        encoding="utf-8")
    (parent / "child.py").write_text("raise RuntimeError('inert child WAS executed')\n",
                                     encoding="utf-8")
    (tmp_path / "cc_probe_namespace_only").mkdir()
    (tmp_path / "cc_probe_module.py").write_text("raise RuntimeError('module WAS executed')\n",
                                                 encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in ("cc_probe_parent", "cc_probe_parent.child", "cc_probe_module"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    return tmp_path


@pytest.mark.parametrize("name,expected", [
    ("cc_probe_parent", True),
    ("cc_probe_parent.child", True),
    ("cc_probe_parent.absent", False),
    ("cc_probe_missing_parent.child", False),
    ("cc_probe_missing_parent", False),
    ("cc_probe_module", True),
    ("cc_probe_module.child", False),
    ("cc_probe_namespace_only", False),
    ("sys", True),
])
def test_discovery_reports_availability_without_executing(build_script, probe_tree, name, expected):
    assert build_script._module_available(name) is expected
    assert "cc_probe_parent" not in sys.modules
    assert "cc_probe_parent.child" not in sys.modules
    assert "cc_probe_module" not in sys.modules


def test_discovery_finds_an_extension_style_child_by_file(build_script, probe_tree):
    ext = probe_tree / "cc_probe_parent" / "native.pyd"
    ext.write_bytes(b"not a real extension; never loaded")
    assert build_script._module_available("cc_probe_parent.native") is (sys.platform == "win32")
    assert "cc_probe_parent" not in sys.modules


def test_build_runs_the_strict_linux_check_before_assembling(build_script, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(sys, "argv", ["build.py"])

    def refuse():
        raise RuntimeError("Linux runtime dependency missing (double)")

    monkeypatch.setattr(build_script, "_require_linux_runtime", refuse)
    monkeypatch.setattr(build_script, "_assemble_command",
                        lambda *a, **k: pytest.fail("assembled before the strict check"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("build started"))
    with pytest.raises(RuntimeError, match="Linux runtime dependency missing"):
        build_script._build()


def test_build_hands_the_assembled_command_to_pyinstaller(build_script, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(sys, "argv", ["build.py", "--onedir"])
    monkeypatch.setattr(build_script, "_require_linux_runtime",
                        lambda: pytest.fail("Linux check ran on Windows"))
    seen = {}

    def assemble_command(onedir, **kwargs):
        seen["onedir"] = onedir
        return ["fake-pyinstaller"]

    monkeypatch.setattr(build_script, "_assemble_command", assemble_command)

    def run(cmd, *a, **k):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    assert build_script._build() == 0
    assert seen["onedir"] is True
    assert seen["cmd"] == ["fake-pyinstaller"]
