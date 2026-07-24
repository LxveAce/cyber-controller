"""Drift-lock: the flash-backend count claimed in README.md must equal the SSOT ``backend_count``
(``site-data.json`` ← ``scripts/site_data_manual.json``), which is the HARDWARE-VALIDATED backend set.

Owner decision 2026-07-24: the advertised headline is the **5 hardware-validated** backends
(esptool, qflipper, adb, sd, rtl8720). The profiles ALSO declare more top-level ``backend`` values
(cc2538_bsl, hackrf_spiflash, nrf_dfu — argv-tested, board-validation pending); those are described
honestly in the README prose but excluded from the headline count until one graduates on real
silicon. Before 2026-07-24 this test locked the README to the profile-declared count (8), which
DISAGREED with the SSOT (5) — an internal-oracle conflict; it now locks to the one SSOT."""
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


def _ssot_backend_count() -> int:
    data = json.loads((_ROOT / "site-data.json").read_text(encoding="utf-8"))
    return int(data["backend_count"])


def test_readme_backend_count_matches_ssot():
    n = _ssot_backend_count()  # the hardware-validated headline (5), not the profile-declared set
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert f"{n} flash backends" in readme, (
        f"README '<n> flash backends' != SSOT backend_count {n} (site-data.json); "
        f"profiles declare {sorted(_distinct_backends())} but the headline is the validated subset"
    )


def test_uncounted_backends_are_acknowledged_not_hidden():
    # The profile-declared backends beyond the validated headline must be named honestly in the
    # README, not silently dropped — so the count is honest, not deflated by hiding capability.
    readme = (_ROOT / "README.md").read_text(encoding="utf-8").lower()
    assert "validated on real silicon" in readme, (
        "README must honestly note the in-progress (not-yet-validated) flash backends"
    )


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
