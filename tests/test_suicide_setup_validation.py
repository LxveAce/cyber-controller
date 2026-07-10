"""Dead Man's Switch gate-config range validation (src/core/suicide_setup.py).

Regression guard (red-team loop-2 finding DMS-2/#7): build() constructs the provisioner argparse
Namespace BY HAND, bypassing the argparse ``choices=`` domain checks, and the provisioner's
validate_args only range-checks a few fields (kdf_iter, max_att, the unsafe pull/level pairing). So an
out-of-range gate field (a mistyped ``arm_level=5`` / ``armed=2`` from the loose run_cli ask() helper
or a programmatic caller) used to reach NVS unchecked and be written as a nonsense u8, making the
firmware misread the gate. build() must now fail LOUD via _validate_cfg BEFORE touching the provisioner.

Mirrors test_suicide_chip.py (fail-loud on a bad chip)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.suicide_setup import SuicideConfig, _validate_cfg, build


def test_validate_cfg_accepts_safe_defaults():
    # The dataclass defaults are the disarmed/safe config — must validate.
    _validate_cfg(SuicideConfig())


def test_validate_cfg_accepts_domain_boundaries():
    # Armed T2-brick at the max attempt/pass counts is a legitimate (if dangerous) config.
    _validate_cfg(SuicideConfig(armed=1, brick=1, arm_level=0, arm_pull=2, max_att=255,
                                sd_passes=255, flash_passes=255, fast_wipe=1))


@pytest.mark.parametrize("bad", [
    {"arm_level": 5}, {"arm_level": -1},
    {"armed": 2}, {"brick": 7}, {"deadman": 3},
    {"arm_pull": 3},
    {"max_att": 999}, {"max_att": -1},
    {"wipe_ota": 2}, {"wipe_nvs": 2}, {"wipe_spiffs": 2}, {"wipe_sd": 2},
    {"sd_passes": 256}, {"flash_passes": 256}, {"fast_wipe": 2},
    {"arm_pin": 99},
])
def test_validate_cfg_rejects_out_of_range(bad):
    with pytest.raises(ValueError):
        _validate_cfg(SuicideConfig(**bad))


def test_validate_cfg_rejects_weak_kdf():
    with pytest.raises(ValueError):
        _validate_cfg(SuicideConfig(kdf_iter=100))


def test_build_fails_loud_on_out_of_range_before_provisioner(tmp_path: Path):
    # build() must reject an out-of-range gate field up front (chip is valid here, so the raise comes
    # from _validate_cfg) rather than forward a nonsense u8 into the baked guardcfg NVS.
    with pytest.raises(ValueError):
        build(SuicideConfig(armed=2), "correct horse", tmp_path / "bundle")
    with pytest.raises(ValueError):
        build(SuicideConfig(arm_level=5), "correct horse", tmp_path / "bundle")
