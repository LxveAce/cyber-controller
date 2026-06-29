"""Equivalence net for the Stage-1 hybrid migration.

`GenericProfile(JSON)` must reproduce the hardcoded `FirmwareProfile` ORACLE class
across every behavior the engine uses — `latest_release`, `variants_for_chip`,
`default_variant`, `app_offset`, `image_model`, `supports_suicide`, and the
`support_files` offset map. Network is mocked (canned `_github_latest`, stubbed
`download_to`). When a profile passes here, swapping `PROFILES[id]` to the
GenericProfile is provably behavior-preserving — this is the gate the flash-argv
golden cannot provide (it never calls the resolver/asset-matching layer).

Profiles are added here as they are converted to the hybrid model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

flash_core = pytest.importorskip("src.core.flash_core")

PROFILES_DIR = Path(__file__).resolve().parents[1] / "src" / "config" / "profiles"
CHIPS = ["esp32", "esp32s2", "esp32s3", "esp32c3", "esp32c6", "esp32c5"]


def _asset(name: str) -> dict:
    return {"name": name, "browser_download_url": f"https://example/{name}"}


# id -> (oracle factory, json filename, canned (tag, raw_assets))
CASES = {
    "bruce": (
        lambda: flash_core.BruceProfile(),
        "bruce.json",
        ("1.15", [
            _asset("Bruce-esp32.bin"),
            _asset("Bruce-LAUNCHER_esp32.bin"),
            _asset("Bruce-esp32-s3-devkitc.bin"),
            _asset("Bruce-m5stack-cardputer.bin"),
            _asset("Bruce-esp32-c5.bin"),
            _asset("Bruce-esp32-c6.bin"),
            _asset("Bruce-LAUNCHER_esp32-s3-devkitc.bin"),
            _asset("README.md"),
            _asset("Bruce-t-deck.bin"),
        ]),
    ),
}


def _load_generic(json_name: str):
    cfg = json.loads((PROFILES_DIR / json_name).read_text(encoding="utf-8"))
    return flash_core.GenericProfile(cfg)


def _support_keys(prof, chip):
    """support_files offset keys (paths differ by stub), or an error marker."""
    try:
        sf = prof.support_files(chip, "CACHE", lambda *a, **k: None)
    except Exception as exc:  # noqa: BLE001
        return ("ERR", type(exc).__name__)
    if sf is None:
        return None
    return sorted(sf.keys())


@pytest.mark.parametrize("pid", sorted(CASES))
def test_generic_matches_oracle(pid, monkeypatch):
    oracle_factory, json_name, (tag, raw) = CASES[pid]
    oracle = oracle_factory()
    generic = _load_generic(json_name)

    monkeypatch.setattr(flash_core, "_github_latest", lambda url: (tag, list(raw)))
    monkeypatch.setattr(flash_core, "download_to", lambda url, cache, name, on_line: f"<f:{name}>")

    # static attributes
    assert generic.id == oracle.id, "id mismatch"
    assert generic.image_model == oracle.image_model, "image_model mismatch"
    assert bool(generic.supports_suicide) == bool(oracle.supports_suicide), "supports_suicide mismatch"

    # release discovery — the layer the golden cannot see
    o_tag, o_assets = oracle.latest_release()
    g_tag, g_assets = generic.latest_release()
    assert g_tag == o_tag, "release tag mismatch"
    assert g_assets == o_assets, (
        "latest_release assets differ:\n"
        f"  oracle ={json.dumps(o_assets, sort_keys=True)}\n"
        f"  generic={json.dumps(g_assets, sort_keys=True)}"
    )

    # per-chip behavior
    for chip in CHIPS:
        assert generic.app_offset(chip) == oracle.app_offset(chip), f"app_offset[{chip}]"
        assert generic.variants_for_chip(g_assets, chip) == oracle.variants_for_chip(o_assets, chip), \
            f"variants_for_chip[{chip}]"
        assert generic.default_variant(g_assets, chip) == oracle.default_variant(o_assets, chip), \
            f"default_variant[{chip}]"
        assert _support_keys(generic, chip) == _support_keys(oracle, chip), f"support_files[{chip}]"
