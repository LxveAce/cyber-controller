"""Drift-lock: the committed site-data.json (the SSOT every LxveLabs website consumes) must match the
live code. If version.py / the profiles glob / the protocol registry changes but nobody regenerates
site-data.json, the sites would silently go stale — this fails CI instead. The web-facing sibling of
tests/test_profile_count.py. Regenerate with: python scripts/gen_site_data.py."""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load_gen():
    spec = importlib.util.spec_from_file_location("gen_site_data", _ROOT / "scripts" / "gen_site_data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_site_data_derived_block_matches_code():
    """The committed site-data.json's DERIVED block equals a fresh regeneration (check() exits 0)."""
    gen = _load_gen()
    assert gen.check() == 0, "site-data.json is stale — run: python scripts/gen_site_data.py"


def test_site_data_internally_consistent():
    gen = _load_gen()
    data = json.loads((_ROOT / "site-data.json").read_text(encoding="utf-8"))

    # profile_count == JSONs on disk (same SSOT as test_profile_count)
    disk_profiles = len(list((_ROOT / "src" / "config" / "profiles").glob("*.json")))
    assert data["profile_count"] == disk_profiles == len(data["profiles"])

    # parser_count == the enumerated parsers, and the generic/raw fallbacks are excluded
    assert data["parser_count"] == len(data["parsers"])
    assert "Generic / Raw" not in data["parsers"]

    # cc_version == src/version.py __version__
    ver_text = (_ROOT / "src" / "version.py").read_text(encoding="utf-8")
    m = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", ver_text)
    assert m and data["cc_version"] == m.group(1)

    # the curated cyber_controller product version tracks the derived cc_version
    assert data["products"]["cyber_controller"] == data["cc_version"]
