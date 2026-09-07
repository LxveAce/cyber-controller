"""Build-shape awareness for the in-place self-updater (owner item #11).

The shipped Windows build is a onedir Inno-Setup install (CyberController.exe + _internal/). The old
self-updater picked the portable onefile and swapped it over the onedir bootstrap, orphaning
_internal/ and corrupting the install. These tests pin: we detect the build shape, and in-place
update refuses early on a onedir build (the UI already falls back to the release page). Nothing
destructive runs.
"""

from __future__ import annotations

import pytest

from src.core import self_update as su
from src.core import updater


# ── installed_kind / can_self_update_in_place ─────────────────────────────────────────────────

def test_installed_kind_source_when_not_frozen(monkeypatch):
    monkeypatch.setattr(su, "is_frozen", lambda: False)
    assert su.installed_kind() == "source"
    assert su.can_self_update_in_place() is False


def test_installed_kind_onedir_when_bundle_under_exe_dir(monkeypatch, tmp_path):
    app = tmp_path / "CyberController"
    app.mkdir()
    exe = app / "CyberController.exe"
    internal = app / "_internal"      # the onedir bundle lives UNDER the exe folder
    internal.mkdir()
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    monkeypatch.setattr(su, "current_exe", lambda: str(exe))
    monkeypatch.setattr(su.sys, "_MEIPASS", str(internal), raising=False)
    assert su.installed_kind() == "onedir"
    assert su.can_self_update_in_place() is False


def test_installed_kind_onefile_when_bundle_is_temp_extraction(monkeypatch, tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    exe = app / "CyberController.exe"
    meipass = tmp_path / "_MEI123456"   # onefile extracts to a temp dir OUTSIDE the exe folder
    meipass.mkdir()
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    monkeypatch.setattr(su, "current_exe", lambda: str(exe))
    monkeypatch.setattr(su.sys, "_MEIPASS", str(meipass), raising=False)
    assert su.installed_kind() == "onefile"
    assert su.can_self_update_in_place() is True


def test_installed_kind_unknown_when_no_meipass(monkeypatch):
    # A frozen build reporting no _MEIPASS is unidentified — we do NOT guess onefile (U3): swap
    # capability must be positively established, not assumed.
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    monkeypatch.delattr(su.sys, "_MEIPASS", raising=False)
    assert su.installed_kind() == "unknown"
    assert su.can_self_update_in_place() is False


def test_installed_kind_onefile_when_exe_run_from_its_extraction_parent(monkeypatch, tmp_path):
    # U2 counterexample: a portable onefile launched from a temp dir has its _MEI… extraction as a
    # child of the exe dir. It must still be onefile — the old ancestor test misread it as onedir.
    exe = tmp_path / "CyberController.exe"
    meipass = tmp_path / "_MEI98765"      # child of the exe dir, but a onefile extraction
    meipass.mkdir()
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    monkeypatch.setattr(su, "current_exe", lambda: str(exe))
    monkeypatch.setattr(su.sys, "_MEIPASS", str(meipass), raising=False)
    assert su.installed_kind() == "onefile"
    assert su.can_self_update_in_place() is True


def test_installed_kind_onedir_legacy_root_layout(monkeypatch, tmp_path):
    # Legacy PyInstaller one-folder: _MEIPASS IS the exe dir (no _internal child).
    app = tmp_path / "CyberController"
    app.mkdir()
    exe = app / "CyberController.exe"
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    monkeypatch.setattr(su, "current_exe", lambda: str(exe))
    monkeypatch.setattr(su.sys, "_MEIPASS", str(app), raising=False)
    assert su.installed_kind() == "onedir"
    assert su.can_self_update_in_place() is False


def test_installed_kind_unknown_when_layout_ambiguous(monkeypatch, tmp_path):
    # Frozen, but the bundle is neither a onedir layout nor a _MEI… extraction → don't guess onedir.
    app = tmp_path / "app"
    app.mkdir()
    exe = app / "CyberController.exe"
    bundle = tmp_path / "some-other-bundle"
    bundle.mkdir()
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    monkeypatch.setattr(su, "current_exe", lambda: str(exe))
    monkeypatch.setattr(su.sys, "_MEIPASS", str(bundle), raising=False)
    assert su.installed_kind() == "unknown"
    assert su.can_self_update_in_place() is False   # unknown is never offered an in-place swap


# ── the real v2.0.x asset catalog (used by the refusal test below) ─────────────────────────────

def _v2_assets():
    return [
        {"name": "cyber-controller-v2.0.1-windows-x64-setup.exe", "browser_download_url": "setup"},
        {"name": "cyber-controller-v2.0.1-windows-x64.exe", "browser_download_url": "portable"},
        {"name": "cyber-controller-v2.0.1-linux-x64", "browser_download_url": "linux"},
        {"name": "SHA256SUMS.txt", "browser_download_url": "sums"},
    ]


# ── in-place update refuses on any non-onefile build (onedir AND unknown), at both boundaries ───

@pytest.mark.parametrize("kind", ["onedir", "unknown"])
def test_self_update_refuses_non_onefile_before_downloading(monkeypatch, kind):
    # U3: onedir AND unknown must be refused BEFORE any download.
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    monkeypatch.setattr(su, "installed_kind", lambda: kind)

    def _boom(*a, **k):
        raise AssertionError("download_asset must not run for a non-onefile build")

    monkeypatch.setattr(su, "download_asset", _boom)
    result = updater.CheckResult(status="NEWER", latest_tag="v2.0.1")
    with pytest.raises(su.SelfUpdateError):
        su.self_update(result, releases=[{"tag_name": "v2.0.1", "assets": _v2_assets()}])


@pytest.mark.parametrize("kind", ["onedir", "unknown"])
def test_apply_refuses_non_onefile_before_dispatch(monkeypatch, tmp_path, kind):
    # U3: onedir AND unknown must be refused BEFORE either platform apply helper runs.
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    monkeypatch.setattr(su, "installed_kind", lambda: kind)

    def _boom(*a, **k):
        raise AssertionError("apply helper must not run for a non-onefile build")

    monkeypatch.setattr(su, "_apply_windows", _boom)
    monkeypatch.setattr(su, "_apply_unix", _boom)
    staged = tmp_path / "cyber-controller-v2.0.1-windows-x64.exe.new"
    staged.write_bytes(b"x")
    with pytest.raises(su.SelfUpdateError):
        su.apply(str(tmp_path / "CyberController.exe"), str(staged), "windows-x64")


# ── the Qt update OFFER is gated on can_self_update_in_place, not is_frozen (U3) ────────────────

@pytest.mark.parametrize("kind,offered", [("onefile", True), ("onedir", False), ("unknown", False)])
def test_update_offer_gates_on_can_self_update_in_place(monkeypatch, kind, offered):
    pytest.importorskip("PyQt5.QtWidgets")
    from unittest.mock import MagicMock

    import src.config.settings as settings_mod
    import src.ui.qt.update_dialog as ud
    from src.core import updater
    from src.ui.qt import main_window as mw

    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"updates": {}})
    monkeypatch.setattr(settings_mod, "save_settings", lambda s: None)
    monkeypatch.setattr(updater, "should_prompt", lambda upd, behind: True)
    monkeypatch.setattr(updater, "apply_update_url", lambda r: "http://example/release")
    monkeypatch.setattr(su, "can_self_update_in_place", lambda: kind == "onefile")

    captured = {}

    class _StubDlg:
        def __init__(self, *a, **k):
            captured["can_self_update"] = k.get("can_self_update")

        def exec_(self):
            return 0

        def action(self):
            return "noop"

        def dont_show_again(self):
            return False

    monkeypatch.setattr(ud, "UpdateAvailableDialog", _StubDlg)

    result = updater.CheckResult(status=updater.NEWER, latest_tag="v9.9.9", behind=3)
    mw.CyberControllerWindow._on_update_check_done(MagicMock(), result, force=True)
    assert captured["can_self_update"] is offered
