"""BatchFlasher must enforce the SHA-256 pin for pinned firmware, same as FlashEngine.

Regression: batch._flash_one downloaded and flashed a pinned app image (bluejammer-esp32, hydra32)
with no verify_sha256 call, so a tampered/MITM'd image bypassed the integrity guard the pin exists for.
Network + hardware fully mocked."""

from __future__ import annotations

import re

from src.core import batch, flash_core


def test_deck_flash_plan_docstring_count_matches_actual():
    """The '(N devices)' claim in the docstring must equal the number of jobs returned.

    Regression: the docstring said '(14 devices)' while the plan returned only 9
    FlashJob entries, so any caller trusting the documented deck size was misled.
    """
    plan = batch.create_deck_flash_plan()
    assert plan and all(isinstance(j, batch.FlashJob) for j in plan)

    doc = batch.create_deck_flash_plan.__doc__ or ""
    m = re.search(r"\((\d+)\s+devices\)", doc)
    assert m, f"docstring must state a '(N devices)' count, got: {doc!r}"
    assert int(m.group(1)) == len(plan), (
        f"docstring claims {m.group(1)} devices but the plan returns {len(plan)}"
    )


def _stub(monkeypatch, tmp_path, sha, verify_raises):
    variant = {"name": "app.bin", "url": "https://github.com/x/app.bin",
               "chip": "esp32", "sha256": sha, "offset": "0x10000"}

    class FakeProfile:
        def latest_release(self):
            return ("v0.2", [variant])

        def default_variant(self, assets, chip):
            return variant

        def support_files(self, chip, cache, on_line):
            return None

        def flash_assets(self, *a, **k):
            return 0

    monkeypatch.setattr(flash_core, "get_profile", lambda pid: FakeProfile())
    monkeypatch.setattr(flash_core, "_detect_chip", lambda port, cap: "esp32")
    monkeypatch.setattr(flash_core, "cache_dir", lambda: str(tmp_path))
    p = tmp_path / "app.bin"
    p.write_bytes(b"bytes")
    monkeypatch.setattr(flash_core, "download_to", lambda u, c, n, cap: str(p))
    seen = []

    def v(path, expected, on_line):
        seen.append((path, expected))
        if verify_raises:
            raise ValueError("SHA-256 mismatch")

    monkeypatch.setattr(flash_core, "verify_sha256", v)
    return seen


def test_batch_aborts_on_pinned_app_sha256_mismatch(monkeypatch, tmp_path):
    seen = _stub(monkeypatch, tmp_path, "d" * 64, verify_raises=True)
    bf = batch.BatchFlasher(on_line=lambda s: None)
    res = bf.flash_sequential([batch.FlashJob(port="COM5", profile_id="bluejammer-esp32")])
    assert seen, "batch must call verify_sha256 for a pinned app image"
    assert res[0].success is False  # tampered image aborts the job


def test_batch_flashes_when_pinned_hash_matches(monkeypatch, tmp_path):
    seen = _stub(monkeypatch, tmp_path, "d" * 64, verify_raises=False)
    bf = batch.BatchFlasher(on_line=lambda s: None)
    res = bf.flash_sequential([batch.FlashJob(port="COM5", profile_id="bluejammer-esp32")])
    assert seen and res[0].success is True


def test_batch_extracts_zip_member_instead_of_flashing_raw_zip(monkeypatch, tmp_path):
    # Parity with FlashEngine: a per-board ZIP bundle (GhostESP) must be EXTRACTED to its merged image, not
    # written raw to 0x0 (which esptool accepts with a warning -> a dead board recorded as success).
    variant = {"name": "GhostESP-board.zip", "url": "https://github.com/x/GhostESP-board.zip",
               "chip": "esp32", "zip_member": "merged.bin"}
    flashed = {}
    calls = {"extract": 0, "download_to": 0}

    class FakeProfile:
        def latest_release(self):
            return ("v1", [variant])

        def default_variant(self, assets, chip):
            return variant

        def support_files(self, chip, cache, on_line):
            return None

        def flash_assets(self, port, chip, app_path, on_line, **k):
            flashed["path"] = app_path
            return 0

    extracted = tmp_path / "merged.bin"
    extracted.write_bytes(b"IMG")
    def _extract(url, cache, asset, member, cap):
        assert member == "merged.bin", f"must extract the declared member, got {member!r}"
        calls["extract"] += 1
        return str(extracted)

    def _download_to(u, c, n, cap):
        calls["download_to"] += 1
        return str(tmp_path / "x")

    monkeypatch.setattr(flash_core, "get_profile", lambda pid: FakeProfile())
    monkeypatch.setattr(flash_core, "_detect_chip", lambda port, cap: "esp32")
    monkeypatch.setattr(flash_core, "cache_dir", lambda: str(tmp_path))
    monkeypatch.setattr(flash_core, "download_and_extract", _extract)
    monkeypatch.setattr(flash_core, "download_to", _download_to)

    bf = batch.BatchFlasher(on_line=lambda s: None)
    res = bf.flash_sequential([batch.FlashJob(port="COM5", profile_id="ghostesp")])
    assert calls["extract"] == 1, "a zip_member variant must be extracted"
    assert calls["download_to"] == 0, "must NOT download the raw .zip as the app image"
    assert flashed.get("path") == str(extracted), "must flash the extracted member, not the archive"
    assert res[0].success is True
