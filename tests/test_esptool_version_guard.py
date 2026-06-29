"""esptool version guard — warn clearly when the installed esptool is out of the supported range.

The flash argv uses esptool's underscore aliases (write_flash/--flash_size/chip_id), which v6 removed.
The pyproject pin enforces esptool>=4.7,<6 for managed installs, but a user's global env can carry an
out-of-range esptool; this guard turns the eventual cryptic argparse failure into a clear message.
"""

from __future__ import annotations

import pytest

flash_core = pytest.importorskip("src.core.flash_core")


@pytest.mark.parametrize("ver,unsupported", [
    ("5.3.0", False),
    ("4.7.0", False),
    ("4.8", False),
    ("6.0.0", True),
    ("6.1.2", True),
    ("3.9", True),
])
def test_unsupported_reason(monkeypatch, ver, unsupported):
    monkeypatch.setattr(flash_core, "esptool_version", lambda: ver)
    reason = flash_core.esptool_unsupported_reason()
    assert (reason is not None) == unsupported
    if unsupported:
        assert "esptool>=4.7,<6" in reason


def test_unknown_version_does_not_block(monkeypatch):
    monkeypatch.setattr(flash_core, "esptool_version", lambda: None)
    assert flash_core.esptool_unsupported_reason() is None


def test_warn_emitted_once_for_esptool_argv(monkeypatch):
    monkeypatch.setattr(flash_core, "_ESPTOOL_VERSION_WARNED", False, raising=False)
    monkeypatch.setattr(flash_core, "esptool_version", lambda: "6.0.0")
    lines: list[str] = []
    flash_core._warn_esptool_version_once(lines.append)
    flash_core._warn_esptool_version_once(lines.append)  # second call must be a no-op
    assert sum(1 for ln in lines if "unsupported" in ln) == 1


def test_no_warn_for_supported_version(monkeypatch):
    monkeypatch.setattr(flash_core, "_ESPTOOL_VERSION_WARNED", False, raising=False)
    monkeypatch.setattr(flash_core, "esptool_version", lambda: "5.3.0")
    lines: list[str] = []
    flash_core._warn_esptool_version_once(lines.append)
    assert lines == []
