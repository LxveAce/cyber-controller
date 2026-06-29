"""Equivalence net for the Stage-1 hybrid migration.

For every profile, `GenericProfile(JSON)` must reproduce the hardcoded `FirmwareProfile`
ORACLE class across every behavior the engine uses — `latest_release`,
`variants_for_chip`, `default_variant`, `app_offset`, `image_model`,
`supports_suicide`, and the `support_files` offset map. Network is mocked (canned
`_github_latest`, stubbed `download_to`). When a profile passes here, swapping
`PROFILES[id]` to the GenericProfile is provably behavior-preserving — the gate the
flash-argv golden cannot provide (it never calls the resolver/asset-matching layer).

Cases (canned release assets per profile) live in `_equiv_cases.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

flash_core = pytest.importorskip("src.core.flash_core")

_HERE = Path(__file__).resolve().parent
PROFILES_DIR = _HERE.parents[0] / "src" / "config" / "profiles"
CASES = json.loads((_HERE / "_equiv_cases.json").read_text(encoding="utf-8"))
CHIPS = ["esp32", "esp32s2", "esp32s3", "esp32c3", "esp32c6", "esp32c5"]


def _support_keys(prof, chip):
    """support_files offset keys (paths differ by stub), or an error marker."""
    try:
        sf = prof.support_files(chip, "CACHE", lambda *a, **k: None)
    except Exception as exc:  # noqa: BLE001 — RuntimeError is a real, oracle-matched outcome
        return ("ERR", type(exc).__name__)
    if sf is None:
        return None
    return sorted(sf.keys())


@pytest.mark.parametrize("pid", sorted(CASES))
def test_generic_matches_oracle(pid, monkeypatch):
    case = CASES[pid]
    oracle = getattr(flash_core, case["oracle_class"])()
    cfg = json.loads((PROFILES_DIR / case["json_file"]).read_text(encoding="utf-8"))
    generic = flash_core.GenericProfile(cfg)

    tag, raw = case["tag"], case["assets"]
    monkeypatch.setattr(flash_core, "_github_latest", lambda url: (tag, [dict(a) for a in raw]))
    monkeypatch.setattr(flash_core, "download_to", lambda url, c, n, o: f"<f:{n}>")

    assert generic.id == oracle.id, f"{pid}: id"
    assert generic.image_model == oracle.image_model, f"{pid}: image_model"
    assert bool(generic.supports_suicide) == bool(oracle.supports_suicide), f"{pid}: supports_suicide"

    o_tag, o_assets = oracle.latest_release()
    g_tag, g_assets = generic.latest_release()
    assert g_tag == o_tag, f"{pid}: release tag"
    assert g_assets == o_assets, (
        f"{pid}: latest_release assets differ:\n"
        f"  oracle ={json.dumps(o_assets, sort_keys=True)}\n"
        f"  generic={json.dumps(g_assets, sort_keys=True)}"
    )

    for chip in CHIPS:
        assert generic.app_offset(chip) == oracle.app_offset(chip), f"{pid}: app_offset[{chip}]"
        assert generic.variants_for_chip(g_assets, chip) == oracle.variants_for_chip(o_assets, chip), \
            f"{pid}: variants_for_chip[{chip}]"
        assert generic.default_variant(g_assets, chip) == oracle.default_variant(o_assets, chip), \
            f"{pid}: default_variant[{chip}]"
        assert _support_keys(generic, chip) == _support_keys(oracle, chip), f"{pid}: support_files[{chip}]"
