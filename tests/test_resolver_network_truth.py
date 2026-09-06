"""GitHub resolver failures must not be relabelled as source-only firmware."""

from __future__ import annotations

import pytest

from src.core import flash_core


def _config() -> dict:
    return {
        "resolver_params": {
            "api_url": "https://api.github.com/repos/example/project/releases/latest",
            "asset_match": {"include_suffixes": [".bin"]},
            "chip_map": {"strategy": "fixed", "chip": "esp32"},
            "on_error": "source_only_empty",
        }
    }


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("release lookup timed out"),
        RuntimeError("GitHub API rate limit"),
        ValueError("unexpected release response"),
    ],
)
def test_source_only_marker_never_swallows_release_fetch_failure(monkeypatch, failure):
    def fail(_url):
        raise failure

    monkeypatch.setattr(flash_core, "_github_latest", fail)

    with pytest.raises(type(failure), match=str(failure)):
        flash_core._resolve_github(_config())


def test_successful_empty_release_remains_distinguishable_from_fetch_failure(monkeypatch):
    monkeypatch.setattr(flash_core, "_github_latest", lambda _url: ("v1.2.3", []))

    tag, assets = flash_core._resolve_github(_config())

    assert tag == "v1.2.3"
    assert assets == []
    assert tag != "source-only"


def test_successful_release_still_emits_matching_artifacts(monkeypatch):
    monkeypatch.setattr(
        flash_core,
        "_github_latest",
        lambda _url: (
            "v1.2.3",
            [
                {"name": "firmware.bin", "browser_download_url": "https://github.com/x/y/a"},
                {"name": "source.zip", "browser_download_url": "https://github.com/x/y/b"},
            ],
        ),
    )

    tag, assets = flash_core._resolve_github(_config())

    assert tag == "v1.2.3"
    assert [asset["name"] for asset in assets] == ["firmware.bin"]
    assert assets[0]["chip"] == "esp32"
