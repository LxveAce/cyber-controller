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

import site
import sys
import sysconfig
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


def _installed_share_roots(primary_base: Path) -> list[Path]:
    """Return the one authoritative setuptools ``data-files`` root.

    A normal/venv install uses the interpreter data prefix, ``--user`` uses the
    user base, and ``--target`` installs the data tree beside ``site-packages``.
    Select the scheme from the package location instead of probing every known
    prefix. This prevents an incomplete target install from silently borrowing
    resources from a different system-wide Cyber Controller version.
    """
    # A checkout owns its repository-relative resources. If one is absent, its
    # expected source path is the useful diagnostic; an installed copy elsewhere
    # must not fill the gap.
    if (primary_base / "pyproject.toml").is_file() or (primary_base / ".git").exists():
        return []

    primary_resolved = primary_base.resolve()
    try:
        user_site = Path(site.getusersitepackages()).resolve()
    except (AttributeError, OSError, TypeError, ValueError):
        user_site = None
    # Equality is intentional. A perfectly valid ``pip --target`` directory can
    # live beneath the user-site tree; ancestry would misclassify that separate
    # installation and let it borrow files from the outer user scheme.
    if user_site is not None and primary_resolved == user_site:
        return [Path(site.getuserbase(), "share", "cyber-controller")]

    for package_path_name in ("purelib", "platlib"):
        try:
            package_root = sysconfig.get_path(package_path_name)
        except (KeyError, TypeError):
            package_root = None
        if not package_root or primary_resolved != Path(package_root).resolve():
            continue

        try:
            data_root = sysconfig.get_path("data")
        except (KeyError, TypeError):
            data_root = None
        if data_root:
            return [Path(data_root, "share", "cyber-controller")]
        return []

    # ``pip --target`` puts both packages and data-file paths under the target.
    # A separate ``--prefix`` install is intentionally not inferred from path
    # spelling alone because it is indistinguishable from a target directory
    # that happens to end in ``site-packages``.
    return [primary_base / "share" / "cyber-controller"]


def resource_path(*parts: str) -> Path:
    """Absolute path to a bundled resource given its repo-relative parts.

    Source checkouts and PyInstaller bundles retain the repository layout under
    :func:`_base_dir`. A normal wheel keeps package data there too, while the
    legacy top-level ``assets`` and ``docs`` directories are installed under the
    active installation scheme's ``share/cyber-controller`` directory. Prefer
    the primary layout whenever the requested resource exists and consult only
    the share root belonging to that same install. For a missing resource,
    return the primary path so callers keep a useful expected location in
    diagnostics.
    """
    primary_base = _base_dir()
    primary = primary_base.joinpath(*parts)
    if primary.exists() or is_frozen():
        return primary
    for root in _installed_share_roots(primary_base):
        installed = root.joinpath(*parts)
        if installed.exists():
            return installed
    return primary
