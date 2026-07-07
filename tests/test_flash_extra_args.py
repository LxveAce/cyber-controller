"""Profile extra_args must reach the esptool write_flash argv — not be silently dropped.

Regression: FirmwareProfile.from_file parses ``extra_args`` from the profile JSON (and the
profile_loader docstring lists it among the fields the engine applies), but nothing consumed it:
FlashEngine._flash_esptool never passed it and flash_core.flash_assets had no extra_args parameter,
so any esptool option a profile supplied (e.g. ``--flash_mode dio``, ``--no-stub``) was ignored.

These tests lock the plumbing at BOTH layers:
  * flash_core.flash_assets splices extra_args into the write_flash argv (base + subclass overrides);
  * FlashEngine._flash_esptool threads profile.extra_args through to core.flash_assets.
esptool is fully mocked — _run_stream / download / release are stubbed, nothing spawns."""

from __future__ import annotations

import pytest

flash_engine = pytest.importorskip("src.core.flash_engine")
from src.core import flash_core  # noqa: E402
from src.core.flash_engine import FirmwareProfile, FlashEngine  # noqa: E402


def _capture_argv(prof, chip="esp32", **kw) -> list[str]:
    """Run prof.flash_assets with _run_stream stubbed, returning the argv it built."""
    captured: dict = {}
    real = flash_core._run_stream

    def fake(argv, on_line):
        captured["argv"] = list(argv)
        return 0

    flash_core._run_stream = fake  # type: ignore[assignment]
    try:
        prof.flash_assets("PORTX", chip, "APP.bin", lambda s: None, **kw)
    finally:
        flash_core._run_stream = real  # type: ignore[assignment]
    return captured["argv"]


def test_base_flash_assets_splices_extra_args_after_write_flash():
    """The golden-locked base flash_assets (used by Marauder et al.) must append extra_args
    tokens as part of the write_flash command."""
    prof = flash_core.get_profile("marauder")  # uses the base FirmwareProfile.flash_assets
    argv = _capture_argv(prof, mode="app", extra_args=["--flash_mode", "dio"])

    assert "--flash_mode" in argv, "extra_args token was dropped from the esptool argv"
    i = argv.index("--flash_mode")
    assert argv[i + 1] == "dio"
    assert argv.index("write_flash") < i, "extra_args must be part of the write_flash command"


def test_generic_profile_forwards_extra_args():
    """GenericProfile (what most shipped JSON profiles resolve to) must forward extra_args to super."""
    gp = flash_core.GenericProfile({"id": "test-fw", "backend": "esptool"})
    argv = _capture_argv(gp, mode="app", extra_args=["--no-stub"])
    assert "--no-stub" in argv


def test_esp32div_override_forwards_extra_args():
    """The Esp32DivProfile override sets flash_freq itself but must still pass extra_args through."""
    div = flash_core.Esp32DivProfile()
    argv = _capture_argv(div, chip="esp32s3", mode="app", extra_args=["--flash_mode", "qio"])
    assert "--flash_mode" in argv and "qio" in argv
    assert "--flash_freq" in argv, "the override's own flash_freq must still be applied"


def test_no_extra_args_leaves_argv_unchanged():
    """Control: with no extra_args the write_flash argv carries no stray tokens (default None)."""
    prof = flash_core.get_profile("marauder")
    argv = _capture_argv(prof, mode="app")
    assert "--flash_mode" not in argv
    assert "--no-stub" not in argv


def test_flash_engine_threads_profile_extra_args_to_flash_assets(monkeypatch, tmp_path):
    """End-to-end: FlashEngine._flash_esptool (download path) must hand profile.extra_args to
    core.flash_assets. Before the fix nothing passed it, so the core saw None."""
    captured: dict = {}

    class FakeCore:
        def latest_release(self):
            return ("v1", [{"name": "app.bin", "url": "https://github.com/x/app.bin", "chip": "esp32"}])

        def variants_for_chip(self, assets, chip):
            return list(assets)

        def default_variant(self, assets, chip):
            return assets[0]

        def app_offset(self, chip):
            return "0x10000"

        def flash_assets(self, port, chip, app_path, on_line, mode="app", baud=921600,
                         support=None, app_offset=None, flash_freq=None, extra_args=None):
            captured["extra_args"] = extra_args
            return 0

    binp = tmp_path / "app.bin"
    binp.write_bytes(b"x")
    monkeypatch.setattr(flash_core, "get_profile", lambda cid: FakeCore())
    monkeypatch.setattr(flash_core, "cache_dir", lambda: str(tmp_path))
    monkeypatch.setattr(flash_core, "download_to", lambda url, cache, name, on_line: str(binp))

    prof = FirmwareProfile(backend="esptool", chip="esp32", core_id="marauder",
                           flash_mode="app", extra_args=["--flash_mode", "dio"])
    ok = FlashEngine().flash("COM7", prof)

    assert ok is True
    assert captured.get("extra_args") == ["--flash_mode", "dio"], \
        "engine must thread profile.extra_args to flash_assets"
