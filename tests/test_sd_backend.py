"""Characterization tests for src/core/backends/sd_backend.py safety logic.

Covers the SSRF host allowlist (_host_allowed / _require_allowed_url), the path-traversal filename guard
(_safe_filename), the removable-drive write gate (_validate_write_target), decompress format dispatch, and
the Pi-profile registry. Pure logic + tmp-file dispatch — no real SD writes.
"""

import pytest

sd = pytest.importorskip("src.core.backends.sd_backend")


# ── _host_allowed ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("host", [
    "github.com", "api.github.com", "raw.githubusercontent.com", "kali.download",
    "x.githubusercontent.com", "foo.kali.download", "GitHub.com", "github.com:443",
])
def test_host_allowed_true(host):
    assert sd._host_allowed(host) is True


@pytest.mark.parametrize("host", [
    None, "", "evil.com", "github.com.evil.com", "github.com@evil.com", "notgithub.com",
])
def test_host_allowed_false(host):
    # NB: "github.com@evil.com" has its userinfo stripped -> host becomes evil.com -> rejected.
    assert sd._host_allowed(host) is False


# ── _require_allowed_url ──────────────────────────────────────────────────
def test_require_allowed_url_ok():
    url = "https://github.com/x/y.img"
    assert sd._require_allowed_url(url) == url


@pytest.mark.parametrize("url", ["", "http://github.com/x", "https://evil.com/x"])
def test_require_allowed_url_rejects(url):
    with pytest.raises(ValueError):
        sd._require_allowed_url(url)


# ── _safe_filename ────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["fw.bin", "kali-linux-2024.1-arm64.img.xz"])
def test_safe_filename_accepts_basename(name):
    assert sd._safe_filename(name) == name


@pytest.mark.parametrize("bad", [
    "", ".", "..", "../evil.img", "..\\evil.img", "/abs/evil.img", "a/b", "a\\b", "C:\\x.img",
])
def test_safe_filename_rejects(bad):
    with pytest.raises(ValueError):
        sd._safe_filename(bad)


# ── _validate_write_target (the removable-drive write gate) ────────────────
def _noline(_l):
    pass


def test_validate_write_target_returns_matching_removable_card():
    card = {"device": "/dev/sdb", "removable": True, "size": 16 * 10**9}
    assert sd._validate_write_target("/dev/sdb", [card], _noline) is card


def test_validate_write_target_refuses_non_removable():
    card = {"device": "/dev/sda", "removable": False, "size": 500 * 10**9}
    with pytest.raises(ValueError):
        sd._validate_write_target("/dev/sda", [card], _noline)


def test_validate_write_target_refuses_at_or_over_cap():
    card = {"device": "/dev/sdb", "removable": True, "size": sd._MAX_SD_BYTES}  # >= cap
    with pytest.raises(ValueError):
        sd._validate_write_target("/dev/sdb", [card], _noline)


def test_validate_write_target_allows_just_under_cap():
    card = {"device": "/dev/sdb", "removable": True, "size": sd._MAX_SD_BYTES - 1}
    assert sd._validate_write_target("/dev/sdb", [card], _noline) is card


def test_validate_write_target_zero_size_skips_cap_check():
    card = {"device": "/dev/sdb", "removable": True, "size": 0}
    assert sd._validate_write_target("/dev/sdb", [card], _noline) is card


def test_validate_write_target_device_not_found():
    cards = [{"device": "/dev/sdb", "removable": True, "size": 0}]
    with pytest.raises(ValueError):
        sd._validate_write_target("/dev/sdz", cards, _noline)


# ── write_image capacity guard (refuse an image larger than the target card) ──
def test_write_image_refuses_image_larger_than_card(tmp_path, monkeypatch):
    img = tmp_path / "big.img"
    img.write_bytes(b"\x00" * 4096)
    dev = r"\\.\PhysicalDrive9"
    monkeypatch.setattr(sd, "detect_sd_cards",
                        lambda on_line: [{"device": dev, "name": "TestCard", "removable": True, "size": 1024}])
    with pytest.raises(ValueError, match="will not fit"):
        sd.write_image(str(img), dev, _noline, confirmed=True)


def test_write_image_allows_image_that_fits(tmp_path, monkeypatch):
    img = tmp_path / "ok.img"
    img.write_bytes(b"\x00" * 512)
    dev = "/dev/sdX"
    monkeypatch.setattr(sd, "detect_sd_cards",
                        lambda on_line: [{"device": dev, "name": "TestCard", "removable": True, "size": 4096}])
    monkeypatch.setattr(sd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sd, "_write_dd", lambda *a, **k: 0)
    assert sd.write_image(str(img), dev, _noline, confirmed=True) == 0


# ── ctypes raw-disk HANDLE marshalling (64-bit safety) ────────────────────────
def test_configure_kernel32_marshals_handle_without_overflow():
    import platform
    if platform.system() != "Windows":
        pytest.skip("kernel32 raw-disk marshalling is Windows-only")
    import ctypes
    k = ctypes.windll.kernel32
    sd._configure_kernel32(k)
    # Opening a non-existent physical drive must return INVALID_HANDLE_VALUE cleanly — no OverflowError
    # from a mis-marshalled pointer-sized handle (the exact bug the argtypes declaration prevents).
    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    h = k.CreateFileW(r"\\.\PhysicalDrive999", GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)
    invalid = ctypes.c_void_p(-1).value
    assert h in (invalid, 0, None)
    if h not in (invalid, 0, None):
        k.CloseHandle(h)


# ── decompress format dispatch ────────────────────────────────────────────
def test_decompress_img_passthrough(tmp_path):
    src = str(tmp_path / "already.img")
    out = sd.decompress(src, str(tmp_path / "out"), lambda _l: None)
    assert out == src  # already an .img -> returned unchanged, no decompression


def test_decompress_unsupported_format_raises(tmp_path):
    with pytest.raises(ValueError):
        sd.decompress(str(tmp_path / "archive.tar"), str(tmp_path / "out"), lambda _l: None)


def test_decompress_xz_dispatch_and_name(tmp_path, monkeypatch):
    calls = {}

    def fake_xz(src, dest, on_line, on_progress):
        calls["dest"] = dest
        return dest

    monkeypatch.setattr(sd, "_decompress_xz", fake_xz)
    out = sd.decompress(str(tmp_path / "kali.img.xz"), str(tmp_path / "out"), lambda _l: None)
    # img_name strips the trailing .xz -> kali.img, routed through _safe_filename into dest_dir.
    assert out.endswith("kali.img")
    assert calls["dest"].endswith("kali.img")


def test_decompress_matches_extension_case_insensitively(tmp_path, monkeypatch):
    # discover_images() compiles file_pattern with re.IGNORECASE, so an asset with an uppercase
    # extension (e.g. 'Raspyjack-v2.0.IMG.XZ') is discovered + downloaded verbatim. decompress must
    # dispatch on it too, not fall through to "unsupported archive format" after the whole download.
    routed = {}

    def fake_xz(src, dest, on_line, on_progress):
        routed["dest"] = dest
        return dest

    monkeypatch.setattr(sd, "_decompress_xz", fake_xz)
    out = sd.decompress(str(tmp_path / "Raspyjack-v2.0.IMG.XZ"), str(tmp_path / "out"), lambda _l: None)
    # dispatched to the xz path (not ValueError); decompressed name keeps its original casing.
    assert out.endswith("Raspyjack-v2.0.IMG")
    assert routed["dest"].endswith("Raspyjack-v2.0.IMG")


def test_decompress_uppercase_img_passthrough(tmp_path):
    src = str(tmp_path / "Already.IMG")
    out = sd.decompress(src, str(tmp_path / "out"), lambda _l: None)
    assert out == src  # uppercase .IMG recognised as already-an-image, returned unchanged


# ── Pi profile registry ───────────────────────────────────────────────────
def test_get_pi_profile_known_and_unknown():
    assert sd.get_pi_profile("pwnagotchi")["repo"] == "jayofelony/pwnagotchi"
    with pytest.raises(KeyError):
        sd.get_pi_profile("nope")


def test_max_sd_bytes_is_256_gib():
    assert sd._MAX_SD_BYTES == 256 * (1 << 30)


# ── download_image atomicity ──────────────────────────────────────────────
def test_download_image_streams_to_temp_not_the_shared_dest(tmp_path, monkeypatch):
    """download_image writes a DETERMINISTIC basename into a process-wide shared cache dir, so two
    concurrent images of the same asset would truncate-interleave the one shared path and silently
    corrupt a bare .img. The fix streams to a unique temp file then os.replace's it into place — so the
    final dest never exists as a partial/torn file. Assert the shared dest does not appear mid-stream
    and the completed file is the full download."""
    url = "https://github.com/o/r/releases/download/v1/pi.img"
    dest = tmp_path / "pi.img"
    seen = {"dest_existed_mid_stream": None}

    class FakeResp:
        is_redirect = False
        is_permanent_redirect = False
        headers = {"content-length": "6"}

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=1):
            for c in (b"abc", b"def"):
                # with the old open(dest,'wb') the shared dest exists (0+ bytes) the moment writing
                # begins; the atomic version writes to a temp file, so dest must NOT exist yet.
                seen["dest_existed_mid_stream"] = dest.exists()
                yield c

    monkeypatch.setattr(sd.requests, "get", lambda *a, **k: FakeResp())

    out = sd.download_image(url, str(tmp_path), _noline)

    assert out == str(dest)
    assert dest.read_bytes() == b"abcdef"            # complete, uncorrupted download
    assert seen["dest_existed_mid_stream"] is False  # streamed to a temp file, not the shared dest


# ── verify read-back: cache bypass + same-privilege (BUGHUNT-0708 #2/#3, Linux) ──────────────────
# The Linux read-back went through the buffered block device (a corrupted write could pass verify from
# RAM), and used an unprivileged open() even though the write ran via `sudo dd` (a good non-root write
# then falsely failed). These lock the helper logic; the on-device behaviour needs a Linux bench.
import hashlib  # noqa: E402
import io  # noqa: E402


def test_hash_reader_reads_exactly_img_size_and_ignores_trailing():
    payload = b"A" * 2500
    reader = io.BytesIO(payload + b"JUNK-BEYOND-THE-IMAGE")
    h = hashlib.sha256()
    seen: list[float] = []
    sd._hash_reader(reader, len(payload), h, seen.append)
    assert h.hexdigest() == hashlib.sha256(payload).hexdigest()  # stops at img_size, ignores trailing
    assert seen and seen[-1] == 1.0


def test_hash_reader_stops_on_short_read():
    reader = io.BytesIO(b"AB")  # device returns fewer bytes than img_size
    h = hashlib.sha256()
    sd._hash_reader(reader, 100, h, None)
    assert h.hexdigest() == hashlib.sha256(b"AB").hexdigest()


def test_drop_block_cache_calls_fadvise_dontneed(monkeypatch):
    calls: list = []
    monkeypatch.setattr(sd.os, "posix_fadvise",
                        lambda fd, off, ln, adv: calls.append((fd, off, ln, adv)), raising=False)
    monkeypatch.setattr(sd.os, "POSIX_FADV_DONTNEED", 4, raising=False)
    sd._drop_block_cache(7, 4096)
    assert calls == [(7, 0, 4096, 4)]  # drops the whole image range from cache -> read hits the media


def test_drop_block_cache_noop_without_fadvise(monkeypatch):
    monkeypatch.delattr(sd.os, "posix_fadvise", raising=False)
    sd._drop_block_cache(7, 4096)  # Windows/macOS lack it -> must be a silent no-op, not a crash


def test_drop_block_cache_swallows_oserror(monkeypatch):
    def boom(*a):
        raise OSError("fadvise not supported here")
    monkeypatch.setattr(sd.os, "posix_fadvise", boom, raising=False)
    monkeypatch.setattr(sd.os, "POSIX_FADV_DONTNEED", 4, raising=False)
    sd._drop_block_cache(7, 4096)  # best-effort: swallow the OSError


class _FakeProc:
    def __init__(self, data: bytes, rc: int = 0):
        self.stdout = io.BytesIO(data)
        self.returncode = rc
        self.waited = False

    def wait(self):
        self.waited = True


def test_verify_read_via_sudo_dd_hashes_privileged_stream(monkeypatch):
    payload = b"Z" * 5000
    captured: dict = {}

    def fake_popen(argv, stdout=None, stderr=None):
        captured["argv"] = argv
        return _FakeProc(payload)

    monkeypatch.setattr(sd.subprocess, "Popen", fake_popen)
    h = hashlib.sha256()
    ok = sd._verify_read_via_sudo_dd("/dev/sdX", len(payload), h, None, lambda ln: None)
    assert ok is True
    assert h.hexdigest() == hashlib.sha256(payload).hexdigest()
    argv = captured["argv"]
    assert argv[0] == "sudo" and argv[1] == "dd"      # read back at write-privilege (#3)
    assert "iflag=direct" in argv                     # ...with the page cache bypassed (#2)
    assert "if=/dev/sdX" in argv


def test_verify_read_via_sudo_dd_fails_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(sd.subprocess, "Popen", lambda *a, **k: _FakeProc(b"", rc=1))
    assert sd._verify_read_via_sudo_dd("/dev/sdX", 0, hashlib.sha256(), None, lambda ln: None) is False


def test_verify_read_via_sudo_dd_fails_when_popen_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("sudo not found")
    monkeypatch.setattr(sd.subprocess, "Popen", boom)
    lines: list[str] = []
    assert sd._verify_read_via_sudo_dd("/dev/sdX", 0, hashlib.sha256(), None, lines.append) is False
    assert any("failed to start" in ln for ln in lines)
