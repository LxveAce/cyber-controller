"""Version-handling tests for src/core/firmware_vault.py.

Covers the two overhaul fixes:
- download_firmware honors a pinned version (queries /releases/tags/{version}, not /releases/latest) and
  rejects an unsafe version tag — a pinned request must never silently resolve to latest.
- check_updates reports the NEWEST cached version by download time, not an arbitrary insertion-order key.

Pure logic: the GitHub API + profile loading are monkeypatched, so no network or real profile JSON is touched.
"""
from __future__ import annotations

import pytest

fwv = pytest.importorskip("src.core.firmware_vault")

_GH = "https://github.com/o/r/releases/latest"


@pytest.fixture
def vault(tmp_path):
    return fwv.FirmwareVault(vault_dir=tmp_path)


def test_download_firmware_honors_pinned_version(vault, monkeypatch):
    captured = {}

    def fake_api(url):
        captured["url"] = url
        return {"tag_name": "v1.2.0", "assets": []}  # no .bin asset -> returns None after recording URL

    monkeypatch.setattr(fwv, "_safe_api_get_json", fake_api)
    monkeypatch.setattr(vault, "_load_profile", lambda pid: {"firmware_urls": {"latest": _GH}})

    assert vault.download_firmware("p", version="v1.2.0") is None
    assert "/releases/tags/v1.2.0" in captured["url"]
    assert "/releases/latest" not in captured["url"]


def test_download_firmware_latest_uses_latest_endpoint(vault, monkeypatch):
    captured = {}

    def fake_api(url):
        captured["url"] = url
        return {"tag_name": "v9", "assets": []}

    monkeypatch.setattr(fwv, "_safe_api_get_json", fake_api)
    monkeypatch.setattr(vault, "_load_profile", lambda pid: {"firmware_urls": {"latest": _GH}})

    vault.download_firmware("p", version="latest")
    assert captured["url"].endswith("/releases/latest")


def test_download_firmware_rejects_unsafe_version(vault, monkeypatch):
    called = {"n": 0}

    def fake_api(url):
        called["n"] += 1
        return {"tag_name": "x", "assets": []}

    monkeypatch.setattr(fwv, "_safe_api_get_json", fake_api)
    monkeypatch.setattr(vault, "_load_profile", lambda pid: {"firmware_urls": {"latest": _GH}})

    assert vault.download_firmware("p", version="../../etc/passwd") is None
    assert called["n"] == 0  # rejected before any API call


def test_check_updates_reports_newest_cached_by_date(vault, monkeypatch):
    # Insertion order deliberately does NOT match date order: the old [-1] bug would report "v1.5".
    vault._index = {"p": {
        "v1.0": {"downloaded_at": "2020-01-01T00:00:00+00:00"},
        "v2.0": {"downloaded_at": "2024-01-01T00:00:00+00:00"},  # newest by date
        "v1.5": {"downloaded_at": "2022-01-01T00:00:00+00:00"},
    }}
    monkeypatch.setattr(vault, "list_profiles", lambda: [{"id": "p", "name": "P"}])
    monkeypatch.setattr(vault, "_load_profile", lambda pid: {"firmware_urls": {"latest": _GH}})
    monkeypatch.setattr(fwv, "_safe_api_get_json", lambda url: {"tag_name": "v3.0"})

    ups = vault.check_updates()
    assert ups and ups[0]["cached_version"] == "v2.0"
    assert ups[0]["latest_version"] == "v3.0"
