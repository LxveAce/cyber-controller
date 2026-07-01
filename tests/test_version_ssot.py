"""Guard: the app version is declared once and never drifts across the three places it surfaces —
``pyproject.toml`` ``[project].version``, :mod:`src.version` (the single source of truth), and the
``src`` package's re-exported ``__version__``.

Regression guard for the drift where ``src/__init__.py`` stayed at ``1.2.1`` while the app shipped
``1.4.0`` (``src/version.py`` + ``pyproject.toml``). If this fails, bump every location together.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import src
from src.version import __version__ as VERSION


def _pyproject_version() -> str:
    root = Path(__file__).resolve().parent.parent
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def test_package_version_reexports_ssot():
    assert src.__version__ == VERSION, "src.__version__ must re-export src.version.__version__"


def test_pyproject_matches_ssot():
    pv = _pyproject_version()
    assert pv == VERSION, f"pyproject.toml version ({pv}) != src/version.py ({VERSION}); bump both together"
