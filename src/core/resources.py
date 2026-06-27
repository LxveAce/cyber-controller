"""Frozen-safe resource path resolution.

Resolves bundled data files identically in a dev checkout and a PyInstaller build.
In a bundle, data is added with dest paths that mirror the repo layout (e.g.
``src/config/profiles``, ``src/ui/qt/theme``), so the same repo-relative path works in
both. Single source of truth for locating shipped resources: do NOT use
``Path(__file__)``-relative paths for bundled data — under ``--onefile`` they point into
the temp extraction dir and miss anything not added with a matching dest (this was the
cause of the silent Windows .exe startup crash: the QSS theme was never bundled).
"""
from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["resource_path", "is_frozen"]


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def _base_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    # dev: src/core/resources.py -> repo root
    return Path(__file__).resolve().parents[2]


def resource_path(*parts: str) -> Path:
    """Absolute path to a bundled resource given its repo-relative parts."""
    return _base_dir().joinpath(*parts)
