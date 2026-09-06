"""Offline startup/build regressions; no Qt windows, operator state or hardware services."""
from __future__ import annotations

import builtins
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import build
from scripts import verify_linux_bundle as verifier
from src import app as entry
from src.core import install, resources
from src.security import access_gate
from src.ui import packaged_smoke


@pytest.mark.parametrize("allowed", [False, True])
def test_headless_default_never_imports_qt_and_preserves_gate(monkeypatch, allowed):
    events = []
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name != "src.ui.launcher" and not name.startswith("PyQt"), name
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(entry, "_setup_logging", lambda *a: None)
    monkeypatch.setattr(install, "reconcile", lambda: None)
    monkeypatch.setattr(entry, "_acquire_instance_lock", lambda: True)

    def gate(ui):
        events.append(("gate", ui))
        return allowed

    def bootstrap():
        assert events == [("gate", "web")] and allowed
        events.append("bootstrap")
        return (SimpleNamespace(shutdown=lambda: events.append("shutdown")), *([None] * 7))

    monkeypatch.setattr(access_gate, "enforce", gate)
    monkeypatch.setattr(entry, "_bootstrap", bootstrap)
    monkeypatch.setitem(entry._LAUNCHERS, "web", lambda *a, **kw: events.append("web") or 0)
    assert entry.main([]) == (0 if allowed else 1)
    assert events == ([("gate", "web"), "bootstrap", "web", "shutdown"] if allowed
                      else [("gate", "web")])


@pytest.mark.parametrize("display", ["DISPLAY", "WAYLAND_DISPLAY"])
def test_display_session_still_gets_chooser_and_gate(monkeypatch, display):
    events = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv(display, "test-display")
    monkeypatch.setitem(sys.modules, "src.ui.launcher", SimpleNamespace(
        select_ui=lambda: events.append("chooser") or "desktop"))
    monkeypatch.setattr(entry, "_setup_logging", lambda *a: None)
    monkeypatch.setattr(install, "reconcile", lambda: None)
    monkeypatch.setattr(entry, "_acquire_instance_lock", lambda: True)
    monkeypatch.setattr(access_gate, "enforce", lambda ui: events.append(("gate", ui)) or False)
    monkeypatch.setattr(entry, "_bootstrap", lambda: pytest.fail("denied gate bootstrapped"))
    assert entry.main([]) == 1
    assert events == ["chooser", ("gate", "desktop")]


def test_packaged_probe_dispatch_precedes_state_and_cli(monkeypatch):
    monkeypatch.setattr(entry, "_parse_args", lambda *a: pytest.fail("normal startup reached"))
    monkeypatch.setattr(packaged_smoke, "run", lambda: 17)
    assert entry.main(["--_smoke-startup"]) == 17


def test_smoke_refuses_source_before_gui_import(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert packaged_smoke.run() == 2


def test_smoke_serves_real_packaged_assets_without_app_operations(monkeypatch):
    monkeypatch.setattr(entry, "_bootstrap", lambda: pytest.fail("hardware bootstrap"))
    app, served = packaged_smoke.make_fixture()
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200 and b'data-view="settings"' in response.data
    for asset in ("reform.css", "reform.js", "vendor/socket.io.min.js"):
        response = client.get("/static/" + asset)
        assert response.status_code == 200 and len(response.data) > 100
    assert served == {"/", "/static/reform.css", "/static/reform.js",
                      "/static/vendor/socket.io.min.js"}
    assert client.get("/api/devices").status_code == 404
    assert client.post("/api/flash", json={}).status_code == 404


def test_smoke_rejects_missing_resource(monkeypatch, tmp_path):
    monkeypatch.setattr(resources, "resource_path", lambda *p: tmp_path.joinpath(*p))
    with pytest.raises(RuntimeError, match="Missing packaged UI resource"):
        packaged_smoke.make_fixture()


@pytest.mark.parametrize("missing", ["qtpy.QtWebEngineWidgets", "flask_socketio",
                                     "cryptography.hazmat.primitives.ciphers.aead"])
def test_linux_freeze_fails_before_invoking_builder(monkeypatch, missing):
    def importer(name):
        if name == missing:
            raise ImportError("synthetic missing dependency")
        return SimpleNamespace()

    monkeypatch.setattr(build.platform, "system", lambda: "Linux")
    monkeypatch.setattr(build.importlib, "import_module", importer)
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k: pytest.fail("builder ran"))
    with pytest.raises(RuntimeError, match=missing):
        build._build()


def _fake_archive(monkeypatch, tmp_path, *, library_version="2.35", machine="EM_X86_64",
                  missing_module=None):
    # Bootloader remains old-compatible; the bundled libpython is the discriminating dependency.
    names = {name: None for name in verifier.QPA_LIBRARIES}
    names.update({"PyQt5/Qt5/plugins/platforms/libqxcb.so": None,
                  "PyQt5/Qt5/libexec/QtWebEngineProcess": None, "libpython3.12.so.1.0": None})
    modules = set(verifier.REQUIRED_MODULES)
    modules.discard(missing_module)
    fake = SimpleNamespace(toc=names, open_embedded_archive=lambda _: SimpleNamespace(toc=modules),
                           extract=lambda name: b"\x7fELFlibrary")
    monkeypatch.setitem(sys.modules, "PyInstaller.archive.readers",
                        SimpleNamespace(CArchiveReader=lambda _: fake))
    monkeypatch.setattr(verifier, "elf_requirements", lambda data:
                        (machine, {"2.14" if data == b"bootloader" else library_version}))
    artifact = tmp_path / "CyberController"
    artifact.write_bytes(b"bootloader")
    return artifact


def test_bundle_accepts_complete_compatible_fixture(monkeypatch, tmp_path):
    artifact = _fake_archive(monkeypatch, tmp_path)
    assert verifier.inspect_bundle(artifact, "EM_X86_64", "2.35")["errors"] == []


def test_bundle_rejects_new_libpython_despite_old_bootloader(monkeypatch, tmp_path):
    artifact = _fake_archive(monkeypatch, tmp_path, library_version="2.38")
    result = verifier.inspect_bundle(artifact, "EM_X86_64", "2.35")
    assert any("libpython3.12.so.1.0 requires GLIBC_2.38" in e for e in result["errors"])
    assert result["elf_glibc_requirements"]["CyberController"] == "2.14"


@pytest.mark.parametrize("fault", ["wrong-architecture", "missing-renderer"])
def test_bundle_rejects_wrong_arch_or_missing_renderer(monkeypatch, tmp_path, fault):
    artifact = _fake_archive(monkeypatch, tmp_path,
                             machine="EM_AARCH64" if fault == "wrong-architecture" else "EM_X86_64",
                             missing_module="qtpy" if fault == "missing-renderer" else None)
    result = verifier.inspect_bundle(artifact, "EM_X86_64", "2.35")
    assert any(("Wrong architecture" if fault == "wrong-architecture" else "Missing required module")
               in error for error in result["errors"])


def test_linux_ci_checks_exact_artifacts_before_upload():
    import yaml
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/build-release.yml"
    jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
    for job, host, machine, floor in (("build-linux", "ubuntu-22.04", "EM_X86_64", "2.35"),
                                     ("build-linux-arm", "ubuntu-24.04-arm", "EM_AARCH64", "2.39")):
        assert jobs[job]["runs-on"] == host
        runs = "\n".join(step.get("run", "") for step in jobs[job]["steps"])
        assert "--no-deps" not in runs
        assert f"--machine {machine} --max-glibc {floor}" in runs
        assert "timeout --kill-after=5s 90s xvfb-run -a env QT_QPA_PLATFORM=xcb" in runs
        assert runs.index("verify_linux_bundle.py") < runs.index("--_smoke-startup")
        assert runs.index("--_smoke-startup") < runs.index("release_upload.sh")
