"""Cyber Controller package.

The application version is defined once in :mod:`src.version` (the single source of truth, kept in
sync with ``pyproject.toml``). It is re-exported here so ``src.__version__`` can never drift from the
SSOT the way a hand-maintained literal did (it was pinned at an old ``1.2.1`` while the app shipped
``1.4.0``).
"""

from .version import __version__

__all__ = ["__version__"]
