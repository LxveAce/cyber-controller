"""FlashEngine._flash_qflipper — the Flipper (Momentum/Unleashed/RogueMaster) path must download the
real package and hand it to qFlipper-cli via qflipper_tool.flash_bundle, and must NEVER report success
without one. Regression for the bug where the shipped download profiles (local_path="") launched a
bare qFlipper and returned rc 0 (and the later one where qFlipper.exe --install, a non-existent flag,
opened the GUI and flashed nothing).

No hardware / network — flash_core and qflipper_tool are stubbed."""

from __future__ import annotations


def test_qflipper_downloads_real_package_and_hands_it_to_the_cli(monkeypatch):
    from src.core import flash_core, qflipper_tool
    from src.core.flash_engine import FlashEngine, FirmwareProfile

    rec = {}

    class FakeCore:
        def latest_release(self):
            return ("v1", [{"name": "fw.tgz", "url": "http://x/fw.tgz", "chip": "flipper"}])

        def variants_for_chip(self, assets, chip):
            return list(assets)

        def default_variant(self, assets, chip):
            return assets[0] if assets else None

    monkeypatch.setattr(flash_core, "PROFILES", {"momentum": object()})
    monkeypatch.setattr(flash_core, "get_profile", lambda cid: FakeCore())
    monkeypatch.setattr(flash_core, "cache_dir", lambda: "/tmp")

    def fake_download(url, cache, name, on_line):
        rec["downloaded"] = (url, name)
        return "/tmp/fw.tgz"

    monkeypatch.setattr(flash_core, "download_to", fake_download)

    # The engine delegates the actual install to qflipper_tool.flash_bundle (headless qFlipper-cli);
    # stub it so no real qFlipper/device is needed, and capture the package path it was handed.
    def fake_flash_bundle(path, on_line, *, allow_provision=False, runner=None):
        rec["flash_app_path"] = path
        rec["got_runner"] = runner is not None
        return 0

    monkeypatch.setattr(qflipper_tool, "flash_bundle", fake_flash_bundle)

    prof = FirmwareProfile(backend="qflipper", core_id="momentum", local_path="", flash_mode="full")
    assert FlashEngine()._flash_qflipper("COM5", prof, None) is True
    assert rec.get("downloaded") == ("http://x/fw.tgz", "fw.tgz")   # a real download happened
    assert rec.get("flash_app_path") == "/tmp/fw.tgz"              # the real package, not bare qFlipper
    assert rec.get("got_runner") is True                          # runs via flash_core._run_stream


def test_qflipper_returns_false_when_cli_flash_fails(monkeypatch):
    """A non-zero qFlipper-cli exit (no device, wrong port, missing CLI) is a failure, never faked."""
    from src.core import flash_core, qflipper_tool
    from src.core.flash_engine import FlashEngine, FirmwareProfile

    class FakeCore:
        def latest_release(self):
            return ("v1", [{"name": "fw.tgz", "url": "http://x/fw.tgz", "chip": "flipper"}])

        def variants_for_chip(self, assets, chip):
            return list(assets)

        def default_variant(self, assets, chip):
            return assets[0] if assets else None

    monkeypatch.setattr(flash_core, "PROFILES", {"momentum": object()})
    monkeypatch.setattr(flash_core, "get_profile", lambda cid: FakeCore())
    monkeypatch.setattr(flash_core, "cache_dir", lambda: "/tmp")
    monkeypatch.setattr(flash_core, "download_to", lambda *a, **k: "/tmp/fw.tgz")
    monkeypatch.setattr(qflipper_tool, "flash_bundle", lambda *a, **k: 1)

    prof = FirmwareProfile(backend="qflipper", core_id="momentum", local_path="", flash_mode="full")
    assert FlashEngine()._flash_qflipper("COM5", prof, None) is False


def test_qflipper_returns_false_when_release_unavailable(monkeypatch):
    from src.core import flash_core
    from src.core.flash_engine import FlashEngine, FirmwareProfile

    class BoomCore:
        def latest_release(self):
            raise RuntimeError("offline")

    monkeypatch.setattr(flash_core, "PROFILES", {"momentum": object()})
    monkeypatch.setattr(flash_core, "get_profile", lambda cid: BoomCore())
    prof = FirmwareProfile(backend="qflipper", core_id="momentum", local_path="", flash_mode="full")
    assert FlashEngine()._flash_qflipper("COM5", prof, None) is False  # no false success


def test_qflipper_returns_false_when_no_profile_and_no_local_package(monkeypatch):
    from src.core import flash_core
    from src.core.flash_engine import FlashEngine, FirmwareProfile

    monkeypatch.setattr(flash_core, "PROFILES", {})  # core_id not resolvable
    prof = FirmwareProfile(backend="qflipper", core_id="momentum", local_path="", flash_mode="full")
    assert FlashEngine()._flash_qflipper("COM5", prof, None) is False
