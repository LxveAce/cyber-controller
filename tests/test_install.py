"""Smart-installation / version-state logic (src/core/install.py).

Classifies existing config vs the running version (fresh/same/upgrade/downgrade/legacy), migrates on
upgrade, and backs up (never deletes) on an overwrite/fresh-start. Isolated via a temp config dir.
"""

from __future__ import annotations

import pytest

install = pytest.importorskip("src.core.install")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    d = tmp_path / ".cyber-controller"
    monkeypatch.setattr(install, "_CONFIG_DIR", d)
    return d


def _seed_state(d, version=None):
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text("{}", encoding="utf-8")  # a state marker
    if version is not None:
        (d / ".installed_version").write_text(version, encoding="utf-8")


def test_fresh_when_no_state(cfg):
    assert install.has_existing_state() is False
    assert install.classify(current="1.4.0") == "fresh"


def test_same_version(cfg):
    _seed_state(cfg, "1.4.0")
    assert install.classify(current="1.4.0") == "same"


def test_upgrade(cfg):
    _seed_state(cfg, "1.3.0")
    assert install.classify(current="1.4.0") == "upgrade"


def test_downgrade(cfg):
    _seed_state(cfg, "1.5.0")
    assert install.classify(current="1.4.0") == "downgrade"


def test_legacy_state_without_version_marker(cfg):
    _seed_state(cfg, version=None)  # state exists, no .installed_version
    assert install.classify(current="1.4.0") == "legacy"


def test_reconcile_records_on_fresh(cfg):
    assert install.reconcile(current="1.4.0") == "fresh"
    assert install.installed_version() == "1.4.0"


def test_reconcile_upgrade_records_new_version(cfg):
    _seed_state(cfg, "1.3.0")
    assert install.reconcile(current="1.4.0") == "upgrade"
    assert install.installed_version() == "1.4.0"  # marker advanced


def test_reconcile_downgrade_keeps_newer_marker(cfg):
    _seed_state(cfg, "1.5.0")
    assert install.reconcile(current="1.4.0") == "downgrade"
    assert install.installed_version() == "1.5.0"  # NOT overwritten — GUI will prompt


def test_backup_moves_aside_not_delete(cfg):
    _seed_state(cfg, "1.5.0")
    bk = install.backup_config_dir()
    assert bk is not None and bk.exists()
    assert (bk / "settings.json").exists()        # data preserved
    assert not cfg.exists()                        # original location cleared
