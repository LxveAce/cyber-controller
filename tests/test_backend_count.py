"""Drift-lock: the flash-backend count claimed in README.md must equal the number of DISTINCT
top-level ``backend`` values across the shipped profiles in src/config/profiles/.

Companion to test_profile_count. The README count had drifted to '5' — the pre-v1.7.0 backend set
(esptool, qflipper, adb, sd, rtl8720) — while v1.7.0 added three new top-level profile backends
(cc2538_bsl, hackrf_spiflash, nrf_dfu), making the real count 8. (uf2 and dfu are flash-engine
handlers emitted dynamically / via flash_method, not a top-level profile ``backend``, so they are
not part of this profile-declared count.)"""
from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _distinct_backends() -> set[str]:
    out: set[str] = set()
    for f in (_ROOT / "src" / "config" / "profiles").glob("*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        out.add(d.get("backend", "esptool"))
    return out


def test_readme_backend_count_matches_profiles():
    backends = _distinct_backends()
    n = len(backends)
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert f"{n} flash backends" in readme, (
        f"README '<n> flash backends' != {n} distinct profile backends {sorted(backends)}"
    )
    assert f"across {n} backends" in readme, f"README 'across <n> backends' != {n}"


def test_every_profile_backend_is_registered():
    """Every backend a profile declares must be a real handler in the flash engine's registry —
    a profile pointing at an unregistered backend would fail to flash at runtime."""
    # The registry keys as defined in src/core/flash_engine.py (kept in sync by this assertion).
    registered = {
        "esptool", "qflipper", "adb", "sd", "sd-image", "rtl8720",
        "cc2538_bsl", "hackrf_spiflash", "dfu", "uf2", "nrf_dfu",
    }
    unknown = _distinct_backends() - registered
    assert not unknown, f"profiles declare backend(s) with no registered handler: {sorted(unknown)}"
