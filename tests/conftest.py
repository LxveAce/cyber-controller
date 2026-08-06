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

    ORDER MATTERS: join every reapable widget's workers FIRST, THEN pump, THEN drop leftovers.
    Pumping before joining is the crash window — a still-running worker's queued cross-thread signal
    to a widget the next test is about to GC segfaults ``processEvents`` (an intermittent native
    EXIT=127/139 with a nondeterministic crash site). Joining first stops new emissions; the pump
    then delivers already-queued signals to still-alive widgets; ``removePostedEvents`` clears what
    is left so a later test's pump can't hit a GC-freed target. Never "fix" this by rewiring a
    worker's ``finished`` to a ``self``-touching lambda — the safe lever is joining at teardown.
    """
    try:
        from PyQt5.QtWidgets import QApplication
    except Exception:  # noqa: BLE001 — PyQt5 not installed: no Qt test ran, nothing to reap
        return
    app = QApplication.instance()
    if app is None:
        return
    # 1) Join workers first (no new cross-thread emissions after this).
    for w in list(app.topLevelWidgets()):
        shut = getattr(w, "shutdown", None)
        if callable(shut):
            try:
                shut()
            except Exception:  # noqa: BLE001 — a reaper must never fail an otherwise-green test
                pass
    # 2) Deliver any already-queued signals to the (still-alive) widgets, then 3) drop the rest so a
    #    later test's processEvents can't hit a GC-freed target.
    app.processEvents()
    app.removePostedEvents(None)


@pytest.fixture(autouse=True)
def _join_leaked_qt_workers():
    """Autouse teardown that runs :func:`reap_qt_workers` after every test (see it for the why)."""
    yield
    reap_qt_workers()


def pytest_sessionfinish(session, exitstatus):
    """Stash the real exit code so :func:`pytest_unconfigure` can force a clean Qt exit with it."""
    session.config._qt_exitstatus = int(exitstatus) if exitstatus is not None else 0


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    """Force a clean process exit after a Qt session, so the exit code reflects the TESTS — never
    Qt's teardown.

    The suite builds many QWidgets + background QThreads. When pytest returns and CPython finalizes,
    Qt destroys a lingering QThread C++ object while its OS thread is still alive — an intermittent
    native SIGSEGV/abort (EXIT=127/139), and sometimes instead a HANG where the process never exits
    (a real, observed leak: dozens of orphaned py/python pairs accumulated over prior runs). The
    per-test reaper joins workers with bounded waits, but interpreter shutdown still races Qt. This
    runs LAST (unconfigure is after the terminal summary printed), so it flushes and ``os._exit``
    with the stashed code — shutdown can't turn a green run red or hang it, and no output is lost.
    Scoped to Qt runs only (no QApplication -> normal exit); skips atexit/coverage, unused here.
    """
    code = getattr(config, "_qt_exitstatus", None)
    if code is None:
        return
    try:
        from PyQt5.QtWidgets import QApplication
    except Exception:  # noqa: BLE001 — no PyQt5 -> no Qt teardown race -> normal exit
        return
    if QApplication.instance() is None:
        return
    import os
    import sys
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 — never let a flush error block the forced exit
        pass
    os._exit(int(code))


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
