"""FlashEngine dfu-util + UF2 (mass-storage) backends — Phase-3 scaffolds.

HW-validation pending: these tests cover the argv/flow (dfu) and detection/copy logic (uf2)
with NO hardware and NO network — flash_core (release fetch + download) and the removable-volume
scan are stubbed, and a tmp_path directory stands in for a mounted UF2 bootloader drive.

Invariant under test (shared with the qFlipper regression): a backend NEVER reports success when
its tool is missing, the release can't be fetched, or nothing was actually resolved/flashed.
"""

from __future__ import annotations


class _FakeCore:
    """Minimal flash_core profile stub: one downloadable asset, chip-agnostic selection."""

    def latest_release(self):
        return ("v1", [{"name": "fw.uf2", "url": "http://x/fw.uf2", "chip": "rp2040"}])

    def variants_for_chip(self, assets, chip):
        return list(assets)

    def default_variant(self, assets, chip):
        return assets[0] if assets else None


def _wire_download(monkeypatch, flash_core, returned_path):
    """Stub the download-or-local resolution deps so _resolve_binary returns *returned_path*."""
    monkeypatch.setattr(flash_core, "PROFILES", {"pico": object()})
    monkeypatch.setattr(flash_core, "get_profile", lambda cid: _FakeCore())
    monkeypatch.setattr(flash_core, "cache_dir", lambda: "/tmp")
    monkeypatch.setattr(
        flash_core, "download_to", lambda url, cache, name, on_line: returned_path)


# ── dfu-util backend ─────────────────────────────────────────────────


def test_dfu_builds_argv_without_id_and_succeeds(monkeypatch):
    from src.core import flash_core, flash_engine
    from src.core.flash_engine import FlashEngine, FirmwareProfile

    rec = {}
    monkeypatch.setattr(flash_engine.shutil, "which", lambda name: "/usr/bin/dfu-util")
    _wire_download(monkeypatch, flash_core, "/tmp/app.bin")

    def fake_run(argv, on_line):
        rec["argv"] = list(argv)
        return 0

    monkeypatch.setattr(flash_core, "_run_stream", fake_run)

    prof = FirmwareProfile(backend="dfu", core_id="pico", local_path="")
    assert FlashEngine()._flash_dfu("AUTO", prof, None) is True
    # No dfu_id set -> no -d flag; alt defaults to 0; -R resets after download.
    assert rec["argv"] == ["dfu-util", "-a", "0", "-D", "/tmp/app.bin", "-R"]


def test_dfu_builds_argv_with_id_and_alt(monkeypatch):
    from src.core import flash_core, flash_engine
    from src.core.flash_engine import FlashEngine, FirmwareProfile

    rec = {}
    monkeypatch.setattr(flash_engine.shutil, "which", lambda name: "/usr/bin/dfu-util")
    _wire_download(monkeypatch, flash_core, "/tmp/app.bin")

    def fake_run(argv, on_line):
        rec["argv"] = list(argv)
        return 0

    monkeypatch.setattr(flash_core, "_run_stream", fake_run)

    prof = FirmwareProfile(backend="dfu", core_id="pico", local_path="",
                           raw={"dfu_alt": 1, "dfu_id": "2e8a:0003"})
    assert FlashEngine()._flash_dfu("AUTO", prof, None) is True
    # dfu_id -> -d VID:PID inserted between the alt setting and the -D download.
    assert rec["argv"] == ["dfu-util", "-a", "1", "-d", "2e8a:0003", "-D", "/tmp/app.bin", "-R"]


def test_dfu_returns_false_on_nonzero_rc(monkeypatch):
    from src.core import flash_core, flash_engine
    from src.core.flash_engine import FlashEngine, FirmwareProfile

    monkeypatch.setattr(flash_engine.shutil, "which", lambda name: "/usr/bin/dfu-util")
    _wire_download(monkeypatch, flash_core, "/tmp/app.bin")
    monkeypatch.setattr(flash_core, "_run_stream", lambda argv, on_line: 1)

    prof = FirmwareProfile(backend="dfu", core_id="pico", local_path="")
    assert FlashEngine()._flash_dfu("AUTO", prof, None) is False


def test_dfu_returns_false_when_dfu_util_missing(monkeypatch):
    from src.core import flash_core, flash_engine
    from src.core.flash_engine import FlashEngine, FirmwareProfile

    called = {"run": False}
    monkeypatch.setattr(flash_engine.shutil, "which", lambda name: None)

    def boom_run(argv, on_line):
        called["run"] = True
        return 0

    monkeypatch.setattr(flash_core, "_run_stream", boom_run)

    prof = FirmwareProfile(backend="dfu", core_id="pico", local_path="")
    # No dfu-util on PATH -> clear install hint + False, and we NEVER shell out (no false success).
    assert FlashEngine()._flash_dfu("AUTO", prof, None) is False
    assert called["run"] is False


def test_dfu_returns_false_when_release_fetch_fails(monkeypatch):
    from src.core import flash_core, flash_engine
    from src.core.flash_engine import FlashEngine, FirmwareProfile

    class _BoomCore:
        def latest_release(self):
            raise RuntimeError("offline")

    monkeypatch.setattr(flash_engine.shutil, "which", lambda name: "/usr/bin/dfu-util")
    monkeypatch.setattr(flash_core, "PROFILES", {"pico": object()})
    monkeypatch.setattr(flash_core, "get_profile", lambda cid: _BoomCore())

    prof = FirmwareProfile(backend="dfu", core_id="pico", local_path="")
    assert FlashEngine()._flash_dfu("AUTO", prof, None) is False  # no false success


# ── UF2 (mass-storage) backend ───────────────────────────────────────


def test_uf2_copies_to_detected_volume(tmp_path, monkeypatch):
    from src.core import flash_core
    from src.core.flash_engine import FlashEngine, FirmwareProfile

    cache = tmp_path / "cache"
    cache.mkdir()
    drive = tmp_path / "drive"
    drive.mkdir()
    (drive / "INFO_UF2.TXT").write_text("UF2 Bootloader v1.0\nModel: Raspberry Pi RP2\n")
    src_uf2 = cache / "fw.uf2"
    src_uf2.write_bytes(b"UF2\x00fake-image")

    _wire_download(monkeypatch, flash_core, str(src_uf2))
    # Stub the removable-volume scan to our fake drive; the REAL INFO_UF2.TXT detection then runs.
    monkeypatch.setattr(FlashEngine, "_uf2_candidate_volumes", lambda self: [str(drive)])

    prof = FirmwareProfile(backend="uf2", core_id="pico", local_path="")
    assert FlashEngine()._flash_uf2("AUTO", prof, None) is True
    copied = drive / "fw.uf2"
    assert copied.is_file()
    assert copied.read_bytes() == b"UF2\x00fake-image"


def test_uf2_uses_explicit_uf2_target_from_raw(tmp_path, monkeypatch):
    from src.core import flash_core
    from src.core.flash_engine import FlashEngine, FirmwareProfile

    cache = tmp_path / "cache"
    cache.mkdir()
    drive = tmp_path / "explicit"
    drive.mkdir()  # no INFO_UF2.TXT — the explicit target must be honored without detection
    src_uf2 = cache / "fw.uf2"
    src_uf2.write_bytes(b"payload")

    _wire_download(monkeypatch, flash_core, str(src_uf2))

    def boom_scan(self):
        raise AssertionError("auto-detect must not run when uf2_target is set")

    monkeypatch.setattr(FlashEngine, "_uf2_candidate_volumes", boom_scan)

    prof = FirmwareProfile(backend="uf2", core_id="pico", local_path="",
                           raw={"uf2_target": str(drive)})
    assert FlashEngine()._flash_uf2("AUTO", prof, None) is True
    assert (drive / "fw.uf2").is_file()


def test_uf2_returns_false_when_no_volume_found(tmp_path, monkeypatch):
    from src.core import flash_core
    from src.core.flash_engine import FlashEngine, FirmwareProfile

    empty = tmp_path / "not-a-uf2-drive"
    empty.mkdir()  # exists but has no INFO_UF2.TXT
    src_uf2 = tmp_path / "fw.uf2"
    src_uf2.write_bytes(b"payload")

    _wire_download(monkeypatch, flash_core, str(src_uf2))
    monkeypatch.setattr(FlashEngine, "_uf2_candidate_volumes", lambda self: [str(empty)])

    prof = FirmwareProfile(backend="uf2", core_id="pico", local_path="")
    assert FlashEngine()._flash_uf2("AUTO", prof, None) is False


def test_uf2_returns_false_when_nothing_to_flash(monkeypatch):
    from src.core import flash_core
    from src.core.flash_engine import FlashEngine, FirmwareProfile

    # core_id not resolvable and no local file -> _resolve_binary returns None before any scan.
    monkeypatch.setattr(flash_core, "PROFILES", {})

    def boom_scan(self):
        raise AssertionError("volume scan must not run when there is nothing to flash")

    monkeypatch.setattr(FlashEngine, "_uf2_candidate_volumes", boom_scan)

    prof = FirmwareProfile(backend="uf2", core_id="pico", local_path="")
    assert FlashEngine()._flash_uf2("AUTO", prof, None) is False


def test_uf2_returns_false_when_release_fetch_fails(monkeypatch):
    from src.core import flash_core
    from src.core.flash_engine import FlashEngine, FirmwareProfile

    class _BoomCore:
        def latest_release(self):
            raise RuntimeError("offline")

    monkeypatch.setattr(flash_core, "PROFILES", {"pico": object()})
    monkeypatch.setattr(flash_core, "get_profile", lambda cid: _BoomCore())

    prof = FirmwareProfile(backend="uf2", core_id="pico", local_path="")
    assert FlashEngine()._flash_uf2("AUTO", prof, None) is False  # no false success


# ── registry wiring ──────────────────────────────────────────────────


def test_backends_registered():
    from src.core.flash_engine import FlashEngine

    eng = FlashEngine()
    assert eng._backends["dfu"] == eng._flash_dfu
    assert eng._backends["uf2"] == eng._flash_uf2
