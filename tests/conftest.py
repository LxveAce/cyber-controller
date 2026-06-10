"""Shared pytest configuration for the cyber-controller test suite.

Puts the repo root on ``sys.path`` so ``import src.*`` works no matter where
pytest is invoked from, and exposes a couple of small path helpers the focused
test modules reuse (the shipped firmware-profile JSON directory).

These tests are designed to run WITHOUT the heavy optional dependencies
(esptool / pyserial / PyQt5 / flask / textual). The modules under test that need
those deps are imported behind ``pytest.importorskip`` in their own test files,
so a missing dep SKIPS rather than errors. ``cryptography`` / ``requests`` /
``psutil`` are assumed present (they are hard runtime deps of the package).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Repo root = the directory that CONTAINS the ``src`` package (one level up from tests/).
_REPO_ROOT = Path(__file__).resolve().parent.parent

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Directory holding the shipped rich firmware-profile JSON files.
PROFILES_DIR = _REPO_ROOT / "src" / "config" / "profiles"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the repository root (contains the ``src`` package)."""
    return _REPO_ROOT


@pytest.fixture(scope="session")
def profiles_dir() -> Path:
    """Absolute path to ``src/config/profiles`` (the shipped JSON profiles)."""
    return PROFILES_DIR


def shipped_profile_paths() -> list[Path]:
    """Return every shipped ``src/config/profiles/*.json`` path (sorted, stable order)."""
    return sorted(PROFILES_DIR.glob("*.json"))
