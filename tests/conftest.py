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


@pytest.fixture(autouse=True)
def _isolate_captures_dir(tmp_path, monkeypatch):
    """Keep EVERY test off the real ``~/.cyber-controller/captures``.

    Window/hub-based tests build the hub with ``captures_persist_path = captures_dir()/captures.json``
    (``main_window.py``), so without this they write placeholder captures into the user's ACTUAL data
    dir — which pollutes app data and flaked ``test_home_summary`` (it reads that same real file). This
    points ``CC_CAPTURES_DIR`` at a fresh per-test tmp dir through ``captures_dir``'s own env-override
    path, so no test touches app data and each test starts with an empty capture store. Tests that
    specifically exercise ``captures_dir`` itself (``test_install``) re-set or clear the env in their own
    body, so this fixture is transparent to them.
    """
    monkeypatch.setenv("CC_CAPTURES_DIR", str(tmp_path / "captures"))


def reap_qt_workers() -> None:
    """Join background QThreads left running on any built-but-never-closed top-level widget.

    Some widgets start a worker QThread at CONSTRUCTION — clearest is ``FlashTab`` whose
    ``_VariantLoader`` hits the network for variants (DeviceTab/CrackLabTab workers are the same
    shape). A test that BUILDS such a widget but never closes it leaves the thread running; when
    CPython later GCs the widget, Qt aborts ("QThread: Destroyed while thread is still running") —
    a native EXIT=127 with no traceback. It is a RACE: random/CI order usually lets a later test's
    ``processEvents`` finish the thread first, so it only bites in fixed order (the intermittent
    flake). Production is correct (``closeEvent`` -> ``shutdown()`` joins every worker); this reaps
    the TEST-only leak so it can't crash a *later* test.

    Cheap: a no-op unless a ``QApplication`` exists; ``processEvents`` first lets a just-finished
    worker self-remove; each ``shutdown()`` only ``wait()``s on a thread that is running, so a
    worker-less widget costs a couple of ``isRunning()`` checks. Never "fix" this by rewiring a
    worker's ``finished`` to a ``self``-touching lambda — the safe lever is joining at teardown.
    """
    try:
        from PyQt5.QtWidgets import QApplication
    except Exception:  # noqa: BLE001 — PyQt5 not installed: no Qt test ran, nothing to reap
        return
    app = QApplication.instance()
    if app is None:
        return
    app.processEvents()
    for w in list(app.topLevelWidgets()):
        shut = getattr(w, "shutdown", None)
        if callable(shut):
            try:
                shut()
            except Exception:  # noqa: BLE001 — a reaper must never fail an otherwise-green test
                pass
    app.processEvents()


@pytest.fixture(autouse=True)
def _join_leaked_qt_workers():
    """Autouse teardown that runs :func:`reap_qt_workers` after every test (see it for the why)."""
    yield
    reap_qt_workers()


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
