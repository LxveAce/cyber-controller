"""FlashEngine._flash_qflipper — the Flipper (Momentum/Unleashed) path must download the real
package and delegate to qFlipper, and must NEVER report success without one. Regression for the bug
where the shipped download profiles (local_path="") launched a bare qFlipper and returned rc 0.

No hardware / network — flash_core is stubbed."""

from __future__ import annotations


def test_qflipper_downloads_real_package_and_never_fakes_success(monkeypatch):
    from src.core import flash_core
    from src.core.flash_engine import FlashEngine, FirmwareProfile

    rec = {}

    class FakeCore:
        def latest_release(self):
            return ("v1", [{"name": "fw.tgz", "url": "http://x/fw.tgz", "chip": "flipper"}])

        def variants_for_chip(self, assets, chip):
            return list(assets)

        def default_variant(self, assets, chip):
            return assets[0] if assets else None

        def flash_assets(self, port, chip, app_path, on_line, mode="app", baud=921600):
            rec["flash_app_path"] = app_path
            rec["flash_chip"] = chip
            return 0

    monkeypatch.setattr(flash_core, "PROFILES", {"momentum": object()})
    monkeypatch.setattr(flash_core, "get_profile", lambda cid: FakeCore())
    monkeypatch.setattr(flash_core, "cache_dir", lambda: "/tmp")

    def fake_download(url, cache, name, on_line):
        rec["downloaded"] = (url, name)
        return "/tmp/fw.tgz"

    monkeypatch.setattr(flash_core, "download_to", fake_download)

    prof = FirmwareProfile(backend="qflipper", core_id="momentum", local_path="", flash_mode="full")
    assert FlashEngine()._flash_qflipper("COM5", prof, None) is True
    assert rec.get("downloaded") == ("http://x/fw.tgz", "fw.tgz")   # a real download happened
    assert rec.get("flash_app_path") == "/tmp/fw.tgz"              # the real package, not bare qFlipper


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
