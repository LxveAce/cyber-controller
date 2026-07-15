"""Tag-pinned firmware_urls resolution for src/core/firmware_vault.py.

A profile's ``firmware_urls`` entry can pin a specific tag in the URL itself — LxveOS points
``latest`` at its rolling ``ci-latest`` PRERELEASE (.../releases/tag/ci-latest). GitHub's
``/releases/latest`` endpoint EXCLUDES prereleases, so an offline "download for offline use" that
defaulted to ``/releases/latest`` would silently resolve a DIFFERENT (or missing) release than the
one the profile points at. download_firmware must honor the tag baked into the URL.

Pure logic: the GitHub API + profile loading are monkeypatched, so no network is touched.
"""
from __future__ import annotations

import pytest

fwv = pytest.importorskip("src.core.firmware_vault")


@pytest.fixture
def vault(tmp_path):
    return fwv.FirmwareVault(vault_dir=tmp_path)


def _capture_api(monkeypatch, seen):
    def fake_api(url):
        seen.append(url)
        return {"tag_name": "ci-latest", "assets": []}  # no .bin -> stops after the API call
    monkeypatch.setattr(fwv, "_safe_api_get_json", fake_api)


def test_tag_pinned_url_resolves_via_releases_tags_not_latest(vault, monkeypatch):
    """A ``/releases/tag/ci-latest`` firmware_urls entry, with no explicit version requested, must
    query ``/releases/tags/ci-latest`` — NOT ``/releases/latest`` (which drops the prerelease)."""
    seen: list[str] = []
    _capture_api(monkeypatch, seen)
    monkeypatch.setattr(vault, "_load_profile", lambda pid: {
        "firmware_urls": {"latest": "https://github.com/LxveAce/lxveos/releases/tag/ci-latest"},
        "image_model": "merged-single-bin"})

    vault.download_firmware("lxveos")  # returns None (no .bin), but only AFTER the resolved query

    assert len(seen) == 1
    assert seen[0].endswith("/repos/LxveAce/lxveos/releases/tags/ci-latest")
    assert not seen[0].endswith("/releases/latest")


def test_plain_latest_url_still_resolves_via_releases_latest(vault, monkeypatch):
    """A plain ``/releases/latest`` URL (no tag baked in) is unchanged — the 49 non-pinned profiles
    keep resolving via ``/releases/latest``, so this fix is scoped to tag-pinned URLs only."""
    seen: list[str] = []
    _capture_api(monkeypatch, seen)
    monkeypatch.setattr(vault, "_load_profile", lambda pid: {
        "firmware_urls": {"latest": "https://github.com/o/r/releases/latest"},
        "image_model": "merged-single-bin"})

    vault.download_firmware("bruce")

    assert len(seen) == 1
    assert seen[0].endswith("/repos/o/r/releases/latest")
