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


def test_check_updates_no_false_positive_for_sanitized_tag(vault, monkeypatch):
    # The index is keyed by the SANITIZED version. download_firmware stores a release whose raw
    # GitHub tag is "2024.1+deb" under the key "2024.1_deb" (re.sub of chars outside [A-Za-z0-9._-]).
    # A later check_updates fetching the RAW tag must NOT report an update for the already-cached fw.
    assert fwv._safe_version_key("2024.1+deb") == "2024.1_deb"
    vault._index = {"p": {
        "2024.1_deb": {"downloaded_at": "2024-01-01T00:00:00+00:00"},
    }}
    monkeypatch.setattr(vault, "list_profiles", lambda: [{"id": "p", "name": "P"}])
    monkeypatch.setattr(vault, "_load_profile", lambda pid: {"firmware_urls": {"latest": _GH}})
    monkeypatch.setattr(fwv, "_safe_api_get_json", lambda url: {"tag_name": "2024.1+deb"})

    ups = vault.check_updates()
    assert ups == []  # already cached -> no false "update available"


def test_check_updates_reports_genuinely_new_sanitized_tag(vault, monkeypatch):
    # Cached "2024.1_deb"; upstream is a genuinely newer "2024.2+deb" -> an update SHOULD be reported.
    vault._index = {"p": {
        "2024.1_deb": {"downloaded_at": "2024-01-01T00:00:00+00:00"},
    }}
    monkeypatch.setattr(vault, "list_profiles", lambda: [{"id": "p", "name": "P"}])
    monkeypatch.setattr(vault, "_load_profile", lambda pid: {"firmware_urls": {"latest": _GH}})
    monkeypatch.setattr(fwv, "_safe_api_get_json", lambda url: {"tag_name": "2024.2+deb"})

    ups = vault.check_updates()
    assert ups and ups[0]["cached_version"] == "2024.1_deb"
    assert ups[0]["latest_version"] == "2024.2+deb"


def test_check_updates_ignores_uncached_profiles(vault, monkeypatch):
    # Only CACHED profiles are checked. A catalog entry you never downloaded has nothing to "update", so it
    # must be skipped AND must trigger NO GitHub call (else check_updates would make one request per catalog
    # entry and report every never-downloaded firmware as an "update").
    vault._index = {"cached_p": {"v1.0": {"downloaded_at": "2024-01-01T00:00:00+00:00"}}}
    calls = {"n": 0}

    def fake_api(url):
        calls["n"] += 1
        return {"tag_name": "v2.0"}

    monkeypatch.setattr(vault, "list_profiles",
                        lambda: [{"id": "cached_p", "name": "Cached"}, {"id": "uncached_p", "name": "Uncached"}])
    monkeypatch.setattr(vault, "_load_profile", lambda pid: {"firmware_urls": {"latest": _GH}})
    monkeypatch.setattr(fwv, "_safe_api_get_json", fake_api)

    ups = vault.check_updates()
    assert [u["profile_id"] for u in ups] == ["cached_p"]   # uncached_p skipped entirely
    assert calls["n"] == 1                                  # exactly ONE call (per cached profile, not per catalog)
    assert ups[0]["cached_version"] == "v1.0"               # a real cached version, never "none"


def test_check_updates_empty_vault_makes_no_calls(vault, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(fwv, "_safe_api_get_json", lambda url: calls.__setitem__("n", calls["n"] + 1) or {})
    assert vault.check_updates() == []
    assert calls["n"] == 0                                  # nothing cached -> no network at all


def test_check_updates_cli_reports_updates(monkeypatch, capsys):
    class _FakeVault:
        def check_updates(self):
            return [{"profile_id": "marauder", "name": "Marauder", "cached_version": "v1.0", "latest_version": "v1.2"}]
    monkeypatch.setattr(fwv, "FirmwareVault", lambda *a, **k: _FakeVault())
    assert fwv.check_updates_cli() == 0
    out = capsys.readouterr().out
    assert "1 cached profile(s)" in out and "Marauder (marauder)" in out and "v1.0 -> latest v1.2" in out


def test_check_updates_cli_nothing_to_report(monkeypatch, capsys):
    monkeypatch.setattr(fwv, "FirmwareVault", lambda *a, **k: type("V", (), {"check_updates": lambda s: []})())
    assert fwv.check_updates_cli() == 0
    assert "up to date" in capsys.readouterr().out
