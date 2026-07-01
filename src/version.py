"""Single source of truth for the application version.

Imported by the UI (window title), the smart-install/version-state logic, and anywhere else that needs
the running version. Keep this in sync with ``pyproject.toml``'s ``version`` on every release.
"""

__version__ = "1.5.0"
