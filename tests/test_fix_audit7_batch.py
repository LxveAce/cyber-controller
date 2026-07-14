"""Regression guards for the cc-deep-audit-7 pass-7 survivors (2026-07-14, ledger pass7).

Three shipped fixes, each re-confirmed against the real code before fixing (verify-never-fake):

E1 (MED, correctness) sd_backend._verify_read_via_sudo_dd — the Linux non-root verify read-back ran
   `dd count=ceil(img_size/_CHUNK)`, emitting up to ~1 MiB MORE than img_size, then closed the pipe
   after hashing exactly img_size. The early close SIGPIPE-killed dd (children reset SIGPIPE to
   SIG_DFL) -> nonzero rc -> a perfectly GOOD write was reported as a verify FAILURE for any image
   whose size isn't a whole number of MiB. Fix: drain dd's tail before closing so it exits cleanly.

E2 (LOW, race) flash_engine._nrf_dfu_pkg_from_hex — built the Nordic DFU package at a FIXED shared
   temp path (cc_nrf_dfu_pkg.zip). The engine flashes different ports in parallel, so two concurrent
   raw-.hex DFU flashes raced on that one file (spurious failure, or worse, the WRONG firmware
   flashed). Fix: a unique per-invocation mkdtemp dir, cleaned up by _flash_nrf_dfu after the write.

E3 (LOW, verify-integrity-gap) adb_backend.install_rayhunter — when a `<zip>.sha256` asset WAS
   published (integrity intended), a verification EXCEPTION (empty/malformed checksum, transient
   fetch error) was swallowed to a warning and the install PROCEEDED on the unverified archive. Fix:
   a check that can't complete is now a hard failure (return 1) — refuse to install unverified.

Pure logic + fakes: no hardware, no real device, no network is touched.
"""
from __future__ import annotations

import hashlib
import os
import shutil

# ── E1: sudo-dd verify drains the tail so a good non-root write is not SIGPIPE-failed ──

class _FakePipeProc:
    """Models a `dd` child writing ``data`` into a pipe. If the reader closes stdout before draining
    all of it, the child's next write hits a broken pipe and dd is SIGPIPE-killed (rc 141) — exactly
    what a real subprocess does (Python resets SIGPIPE to SIG_DFL in children). A fully-drained pipe
    lets dd finish and exit 0."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0
        self.returncode = None
        self.stdout = self

    def read(self, n: int = -1) -> bytes:
        chunk = self._data[self._pos:] if n < 0 else self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        pass

    def wait(self) -> None:
        self.returncode = 0 if self._pos >= len(self._data) else 141


def test_verify_read_via_sudo_dd_drains_tail_so_good_write_is_not_sigpipe_failed(monkeypatch):
    from src.core.backends import sd_backend as sd

    img_size = sd._CHUNK + 512  # NOT a whole MiB -> dd emits ceil()=2 full blocks (2 MiB), a tail
    blocks = (img_size + sd._CHUNK - 1) // sd._CHUNK
    total = blocks * sd._CHUNK
    payload = b"Z" * img_size
    proc = _FakePipeProc(payload + b"\x00" * (total - img_size))
    monkeypatch.setattr(sd.subprocess, "Popen", lambda *a, **k: proc)

    h = hashlib.sha256()
    ok = sd._verify_read_via_sudo_dd("/dev/sdX", img_size, h, None, lambda ln: None)
    assert ok is True                       # a GOOD non-root write must verify, not SIGPIPE-fail
    assert proc.returncode == 0             # dd fully drained -> clean exit (bug left it 141)
    assert h.hexdigest() == hashlib.sha256(payload).hexdigest()  # hashes only img_size bytes


# ── E2: nrf DFU package build uses a unique dir per call + is cleaned up ──

def test_nrf_dfu_pkg_from_hex_uses_unique_path_per_call(monkeypatch, tmp_path):
    from src.core import flash_core
    from src.core.flash_engine import FlashEngine

    def fake_run(argv, on_line):
        out = argv[-1]  # out_zip is the last arg for both the adafruit and classic tool shapes
        with open(out, "wb") as f:
            f.write(b"PK\x03\x04")
        return 0

    monkeypatch.setattr(flash_core, "_run_stream", fake_run)
    hexf = tmp_path / "fw.hex"
    hexf.write_text(":00000001FF\n")
    eng = FlashEngine()
    p1 = eng._nrf_dfu_pkg_from_hex("/usr/bin/adafruit-nrfutil", str(hexf), {}, lambda s: None)
    p2 = eng._nrf_dfu_pkg_from_hex("/usr/bin/adafruit-nrfutil", str(hexf), {}, lambda s: None)
    try:
        assert p1 and p2 and p1 != p2       # each build gets its OWN dir — no shared-temp race
        assert os.path.isfile(p1) and os.path.isfile(p2)
    finally:
        for p in (p1, p2):
            if p:
                shutil.rmtree(os.path.dirname(p), ignore_errors=True)


def test_flash_nrf_dfu_cleans_up_generated_pkg_dir(monkeypatch):
    from src.core import flash_core, flash_engine
    from src.core.flash_engine import FirmwareProfile, FlashEngine

    monkeypatch.setattr(flash_engine.shutil, "which",
                        lambda name: "/usr/bin/adafruit-nrfutil" if "adafruit" in name else None)
    # _resolve_binary returns a raw .hex, so the genpkg pre-step runs and builds a temp package.
    monkeypatch.setattr(FlashEngine, "_resolve_binary",
                        lambda self, profile, on_line, kind: "/tmp/fw.hex")
    seen: dict = {}

    def fake_run(argv, on_line):
        if "genpkg" in argv:  # the pkg-build step: create the out_zip and record its unique dir
            out = argv[-1]
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(b"PK\x03\x04")
            seen["pkg_dir"] = os.path.dirname(out)
        return 0

    monkeypatch.setattr(flash_core, "_run_stream", fake_run)
    prof = FirmwareProfile(backend="nrf_dfu", core_id="pico", local_path="",
                           raw={"nrf_dfu_tool": "adafruit-nrfutil",
                                "nrf_dfu_pkg_generate": {"hw_version": 52}})
    ok = FlashEngine()._flash_nrf_dfu("COM7", prof, None)
    assert ok is True
    assert "pkg_dir" in seen                     # the raw-.hex genpkg pre-step actually ran
    assert not os.path.isdir(seen["pkg_dir"])    # the unique temp dir is removed after the flash


# ── E3: install_rayhunter refuses to proceed when a published checksum can't be verified ──

def test_install_rayhunter_refuses_when_published_checksum_unverifiable(monkeypatch, tmp_path):
    from src.core.backends import adb_backend as adb

    zip_name = "rayhunter-x86_64.zip"
    assets = [
        {"name": zip_name, "browser_download_url": "https://x.example/r.zip"},
        {"name": zip_name + ".sha256", "browser_download_url": "https://x.example/r.zip.sha256"},
    ]
    monkeypatch.setattr(adb, "_github_latest", lambda repo: ("v1.0", assets))
    monkeypatch.setattr(adb, "_pick_platform_asset", lambda a: assets[0])
    monkeypatch.setattr(adb, "cache_dir", lambda: str(tmp_path))
    zip_path = tmp_path / zip_name
    zip_path.write_bytes(b"not a real zip")
    monkeypatch.setattr(adb, "_download_to", lambda url, cache, name, on_line: str(zip_path))
    # A published but empty/malformed checksum: expected_raw.split()[0] raises IndexError.
    monkeypatch.setattr(adb, "_http_get", lambda url: b"")
    called = {"extract": False, "run": False}
    monkeypatch.setattr(adb, "_extract_zip", lambda *a, **k: called.__setitem__("extract", True))
    monkeypatch.setattr(adb, "_run_adb",
                        lambda *a, **k: (called.__setitem__("run", True), (0, ""))[1])

    lines: list[str] = []
    rc = adb.install_rayhunter(lines.append)
    assert rc == 1  # a published checksum we can't verify -> refuse to install
    assert called == {"extract": False, "run": False}  # must NOT install the unverified archive
    assert any("unverified" in ln.lower() for ln in lines)
