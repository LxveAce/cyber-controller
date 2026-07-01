"""BatchFlasher must enforce the SHA-256 pin for pinned firmware, same as FlashEngine.

Regression: batch._flash_one downloaded and flashed a pinned app image (bluejammer-esp32, hydra32)
with no verify_sha256 call, so a tampered/MITM'd image bypassed the integrity guard the pin exists for.
Network + hardware fully mocked."""

from __future__ import annotations

from src.core import batch, flash_core


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
