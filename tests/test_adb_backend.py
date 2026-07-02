"""Characterization tests for src/core/backends/adb_backend.py pure logic.

Covers the (github-only) host allowlist, filename guard, release-asset selection by OS/arch
(_pick_platform_asset), and the version-compare in check_version. Subprocess/network calls are monkeypatched.
"""

import pytest

adb = pytest.importorskip("src.core.backends.adb_backend")


# ── _host_allowed (narrower than sd_backend — github only, no kali) ────────
@pytest.mark.parametrize("host,ok", [
    ("github.com", True),
    ("api.github.com", True),
    ("x.githubusercontent.com", True),
    ("kali.download", False),        # allowed for sd_backend, NOT for adb
    ("foo.kali.download", False),
    ("evil.com", False),
    (None, False),
])
def test_host_allowed(host, ok):
    assert adb._host_allowed(host) is ok


def test_safe_filename_accepts_and_rejects():
    assert adb._safe_filename("rayhunter.zip") == "rayhunter.zip"
    with pytest.raises(ValueError):
        adb._safe_filename("../rayhunter.zip")


# ── _pick_platform_asset ──────────────────────────────────────────────────
def _set_platform(monkeypatch, system, machine):
    monkeypatch.setattr(adb.platform, "system", lambda: system)
    monkeypatch.setattr(adb.platform, "machine", lambda: machine)


def test_pick_asset_matches_os_and_arch(monkeypatch):
    _set_platform(monkeypatch, "Linux", "x86_64")
    assets = [{"name": "rayhunter-linux-x86_64.zip", "browser_download_url": "u1"}]
    assert adb._pick_platform_asset(assets)["browser_download_url"] == "u1"


def test_pick_asset_os_mismatch_returns_none(monkeypatch):
    _set_platform(monkeypatch, "Windows", "x86_64")
    assets = [{"name": "rayhunter-linux-x86_64.zip", "browser_download_url": "u1"}]
    assert adb._pick_platform_asset(assets) is None


def test_pick_asset_prefers_more_specific_arch(monkeypatch):
    _set_platform(monkeypatch, "Linux", "arm64")
    assets = [
        {"name": "app-linux-arm.zip", "browser_download_url": "arm"},
        {"name": "app-linux-aarch64.zip", "browser_download_url": "aarch64"},
    ]
    # arch order for arm64 is [aarch64, arm64, arm] -> aarch64 scores highest.
    assert adb._pick_platform_asset(assets)["browser_download_url"] == "aarch64"


def test_pick_asset_ignores_non_zip(monkeypatch):
    _set_platform(monkeypatch, "Linux", "x86_64")
    assert adb._pick_platform_asset([{"name": "notes-linux-x86_64.txt"}]) is None


def test_pick_asset_empty_list(monkeypatch):
    _set_platform(monkeypatch, "Linux", "x86_64")
    assert adb._pick_platform_asset([]) is None


# ── check_version compare (installed vs latest) ────────────────────────────
def _patch_versions(monkeypatch, installed, latest_tag):
    monkeypatch.setattr(adb, "installed_version", lambda *a, **k: installed)
    monkeypatch.setattr(adb, "latest_version", lambda *a, **k: (latest_tag, "url"))


def test_check_version_equal_no_update(monkeypatch):
    _patch_versions(monkeypatch, "v1.2", "1.2")  # leading 'v' stripped on both sides -> equal
    assert adb.check_version(lambda _l: None)["update_available"] is False


def test_check_version_differ_update(monkeypatch):
    _patch_versions(monkeypatch, "1.0", "2.0")
    assert adb.check_version(lambda _l: None)["update_available"] is True


def test_check_version_no_installed(monkeypatch):
    _patch_versions(monkeypatch, None, "2.0")
    assert adb.check_version(lambda _l: None)["update_available"] is False


def test_check_version_no_latest(monkeypatch):
    _patch_versions(monkeypatch, "1.0", None)
    assert adb.check_version(lambda _l: None)["update_available"] is False


# ── latest_version unknown profile (pure) + registry drift-lock ────────────
def test_latest_version_unknown_profile():
    assert adb.latest_version("does-not-exist") == (None, None)


def test_adb_profiles_has_rayhunter():
    assert adb.ADB_PROFILES["rayhunter"]["repo"] == "EFForg/rayhunter"
