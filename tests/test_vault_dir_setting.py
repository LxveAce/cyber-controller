"""The 'Vault Directory' setting must actually relocate the firmware cache.

Regression: settings['vault']['dir'] was persisted but never consumed — every FirmwareVault was built
with the hardcoded default. configured_vault_dir() now reads the setting and the three construction sites
pass it, so a user who points the vault at another directory caches firmware there.
"""

from __future__ import annotations

from pathlib import Path

from src.core import firmware_vault as fv


def test_configured_vault_dir_reads_setting(monkeypatch, tmp_path):
    target = tmp_path / "custom_fw"
    # load_settings is imported lazily inside configured_vault_dir, so patch it on the source module.
    import src.config.settings as st
    monkeypatch.setattr(st, "load_settings", lambda: {"vault": {"dir": str(target)}})
    assert fv.configured_vault_dir() == target
    vault = fv.FirmwareVault(fv.configured_vault_dir())
    assert vault.vault_dir == target
    assert target.is_dir()  # the chosen directory was actually created/used


def test_configured_vault_dir_blank_falls_back_to_default(monkeypatch):
    import src.config.settings as st
    monkeypatch.setattr(st, "load_settings", lambda: {"vault": {"dir": "   "}})
    assert fv.configured_vault_dir() == fv._DEFAULT_VAULT_DIR


def test_default_setting_matches_real_vault_dir(monkeypatch):
    # The persisted default and the code's default must agree (they diverged before this fix).
    from src.config.settings import DEFAULTS
    assert Path(DEFAULTS["vault"]["dir"]).expanduser() == fv._DEFAULT_VAULT_DIR
