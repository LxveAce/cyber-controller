"""Upgrade-path evidence for src/core/install.py against a synthetic older profile in a temp home.

install.reconcile() decides version state and records the marker; the profile content itself is
carried by settings.load_settings()'s deep-merge. These tests exercise both together on the same
isolated config dir (install._CONFIG_DIR and settings.SETTINGS_DIR/SETTINGS_PATH point at one
temp home), plus the migration boundary predicate and the backup collision suffix, none of which
tests/test_install.py reaches. No real ~/.cyber-controller is touched.
"""
from __future__ import annotations

import json

import pytest

from src.config import settings
from src.core import install

OLD = "1.3.0"
NEW = "2.0.1"

OLDER_PROFILE = {
    "flash": {"mode": "qio", "verify": False},
    "interface": {"mode": "simple"},
    "uploads": {"wigle_token": "synthetic-not-a-real-token"},
    "_disclaimer_ack": True,
    "future_section": {"knob": 7},
    "future_scalar": "kept-verbatim",
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / ".cyber-controller"
    monkeypatch.setattr(install, "_CONFIG_DIR", d)
    monkeypatch.setattr(settings, "SETTINGS_DIR", d)
    monkeypatch.setattr(settings, "SETTINGS_PATH", d / "settings.json")
    return d


def seed(d, version, profile=OLDER_PROFILE):
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
    if version is not None:
        (d / ".installed_version").write_text(version, encoding="utf-8")
    return (d / "settings.json").read_bytes()


def assert_profile_carried_forward(loaded):
    assert loaded["flash"]["mode"] == "qio"
    assert loaded["flash"]["verify"] is False
    assert loaded["flash"]["auto_backup"] is True, "a key the old profile lacked gets its default"
    assert loaded["interface"]["mode"] == "simple"
    assert loaded["interface"]["touch_mode"] == "auto"
    assert loaded["uploads"]["wigle_token"] == "synthetic-not-a-real-token"
    assert loaded["uploads"]["wdgwars_token"] == ""
    assert loaded["_disclaimer_ack"] is True
    assert loaded["future_section"] == {"knob": 7}
    assert loaded["future_scalar"] == "kept-verbatim"
    assert loaded["safety"] == settings.DEFAULTS["safety"], "an untouched section is the default"


# Upgrade, legacy and downgrade keep the profile content.

def test_upgrade_carries_the_older_profile_forward(home):
    before = seed(home, OLD)
    assert install.reconcile(current=NEW) == "upgrade"
    assert install.installed_version() == NEW
    assert (home / "settings.json").read_bytes() == before, "reconcile never rewrites settings"
    assert_profile_carried_forward(settings.load_settings())


def test_legacy_profile_without_marker_is_carried_forward(home):
    before = seed(home, None)
    assert install.reconcile(current=NEW) == "legacy"
    assert install.installed_version() == NEW
    assert (home / "settings.json").read_bytes() == before
    assert_profile_carried_forward(settings.load_settings())


def test_downgrade_touches_neither_marker_nor_profile(home):
    before = seed(home, "9.9.9")
    assert install.reconcile(current=NEW) == "downgrade"
    assert install.installed_version() == "9.9.9"
    assert (home / "settings.json").read_bytes() == before
    assert_profile_carried_forward(settings.load_settings())


def test_same_version_start_leaves_profile_bytes_alone(home):
    before = seed(home, NEW)
    assert install.reconcile(current=NEW) == "same"
    assert (home / "settings.json").read_bytes() == before


# Back up and start fresh: the old profile survives in the backup, the new dir starts from defaults.

def test_backup_then_record_starts_fresh_and_keeps_old_profile_in_backup(home):
    before = seed(home, "9.9.9")
    backup = install.backup_config_dir()
    install.record_version(NEW)
    assert backup is not None and (backup / "settings.json").read_bytes() == before
    assert (backup / ".installed_version").read_text(encoding="utf-8") == "9.9.9"
    assert install.installed_version() == NEW
    assert not (home / "settings.json").exists()
    assert settings.load_settings() == settings._defaults_copy()
    assert install.has_existing_state() is False, "a marker alone is not existing state"
    assert install.classify(current=NEW) == "fresh"


def test_backup_collision_gets_a_suffix_and_keeps_both(home, monkeypatch):
    class FixedNow:
        @staticmethod
        def strftime(fmt):
            return "20260907-000000"

    class FixedDatetime:
        @staticmethod
        def now():
            return FixedNow()

    monkeypatch.setattr(install, "datetime", FixedDatetime)
    seed(home, OLD)
    earlier = home.with_name(home.name + ".bak.20260907-000000")
    earlier.mkdir()
    (earlier / "settings.json").write_text('{"earlier": true}', encoding="utf-8")

    backup = install.backup_config_dir()

    assert backup is not None
    assert backup.name == home.name + ".bak.20260907-000000-1"
    assert json.loads((earlier / "settings.json").read_text(encoding="utf-8")) == {"earlier": True}
    assert json.loads((backup / "settings.json").read_text(encoding="utf-8")) == OLDER_PROFILE
    assert not home.exists()


# Migration engine: fires across the boundary only, survives a failing migration.

def _recorders(monkeypatch, *boundaries, failing=()):
    calls = []

    def make(boundary):
        def migrate():
            if boundary in failing:
                raise RuntimeError(f"migration {boundary} failed (synthetic)")
            calls.append(boundary)
        return migrate

    monkeypatch.setattr(install, "_MIGRATIONS", [(b, make(b)) for b in boundaries])
    return calls


@pytest.mark.parametrize("from_v,to_v,expected", [
    ("1.3.0", "1.4.0", ["1.4.0"]),
    ("1.3.0", "2.0.1", ["1.4.0", "2.0.0"]),
    ("1.4.0", "1.5.0", []),
    ("1.2.0", "1.3.0", []),
    ("2.0.0", "2.0.1", []),
    (None, "2.0.1", ["1.4.0", "2.0.0"]),
])
def test_run_migrations_fires_only_across_the_boundary(monkeypatch, from_v, to_v, expected):
    calls = _recorders(monkeypatch, "1.4.0", "2.0.0")
    assert install.run_migrations(from_v, to_v) == expected
    assert calls == expected


def test_failing_migration_is_swallowed_and_later_ones_still_run(monkeypatch):
    calls = _recorders(monkeypatch, "1.4.0", "2.0.0", failing=("1.4.0",))
    assert install.run_migrations("1.3.0", "2.0.1") == ["2.0.0"]
    assert calls == ["2.0.0"]


@pytest.mark.parametrize("stored,expected_status,expect_migration", [
    (OLD, "upgrade", True),
    (None, "legacy", True),
    (NEW, "same", False),
    ("9.9.9", "downgrade", False),
])
def test_reconcile_runs_migrations_only_when_moving_forward(home, monkeypatch, stored,
                                                             expected_status, expect_migration):
    calls = _recorders(monkeypatch, "2.0.0")
    seed(home, stored)
    assert install.reconcile(current=NEW) == expected_status
    assert calls == (["2.0.0"] if expect_migration else [])
    assert_profile_carried_forward(settings.load_settings())


def test_fresh_start_runs_no_migration(home, monkeypatch):
    calls = _recorders(monkeypatch, "2.0.0")
    assert install.reconcile(current=NEW) == "fresh"
    assert calls == []
    assert install.installed_version() == NEW
