"""Tests for ``src.core.backends.rtl8720_backend`` — the BW16 / RTL8720DN flash backend.

All tests are pure: the module under test imports only the standard library, so no
hardware, pyserial, esptool, or a real ``rtltool.py`` is required. Every subprocess
invocation is mocked (``subprocess.Popen`` is replaced with a fake), and tool discovery is
monkeypatched so no real file system layout is needed.

Covered:
    * tool discovery: explicit path, bundled dir, PATH, and "not found";
    * ``rtltool_available`` reflects discovery;
    * the ``rf`` (read/dump) argv: correct offset (0x0), size, baud, and out path;
    * the ``wf`` (write) argv: correct offset, image, baud;
    * ``.py`` tools are invoked via the current interpreter; exe tools directly;
    * ``--flash-loader`` is added only when a stub path is supplied/found;
    * missing tool raises :class:`RtlToolNotFound` with install guidance;
    * a missing image raises ``FileNotFoundError`` BEFORE the tool is invoked;
    * streamed lines reach the ``on_line`` callback;
    * download-mode help is emitted, and a no-sync failure is annotated;
    * the "unprotect" gotcha is surfaced;
    * ``flash`` performs the DUMP (read) BEFORE the WRITE;
    * ``flash`` ABORTS (no write) when the pre-write backup fails or is empty;
    * ``flash`` with ``skip_backup=True`` does NOT dump first;
    * baud / size defaults match the documented hardware facts.
"""

from __future__ import annotations

import os

import pytest

rtl = pytest.importorskip("src.core.backends.rtl8720_backend")


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #

class _FakePopen:
    """Stand-in for subprocess.Popen that yields canned output lines and an exit code.

    Records the argv it was constructed with on the class so tests can assert the
    command line built by the backend.
    """

    last_argv: list | None = None
    instances: list = []

    #: configured per-test: list of stdout lines (without newlines) and the return code.
    out_lines: list = []
    returncode_value: int = 0

    def __init__(self, argv, stdout=None, stderr=None, stdin=None, text=None, bufsize=None):
        type(self).last_argv = list(argv)
        type(self).instances.append(list(argv))
        self._lines = list(type(self).out_lines)
        self.returncode = type(self).returncode_value
        # emulate a line-iterable text-mode stdout
        self.stdout = iter([ln + "\n" for ln in self._lines])

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):  # pragma: no cover - only hit on the exception/timeout paths
        pass


def _install_fake_popen(monkeypatch, out_lines=None, returncode=0):
    """Patch the backend's subprocess.Popen with a configured _FakePopen subclass."""
    klass = type("_FP", (_FakePopen,), {
        "last_argv": None,
        "instances": [],
        "out_lines": list(out_lines or []),
        "returncode_value": returncode,
    })
    monkeypatch.setattr(rtl.subprocess, "Popen", klass)
    return klass


class _Collector:
    """Simple on_line sink that records every streamed line."""

    def __init__(self):
        self.lines: list = []

    def __call__(self, s):
        self.lines.append(s)

    @property
    def text(self):
        return "\n".join(self.lines)


def _force_tool(monkeypatch, path="/fake/rtltool.py"):
    """Make find_rtltool always return *path* and find_flash_loader return None."""
    monkeypatch.setattr(rtl, "find_rtltool", lambda explicit=None: path)
    monkeypatch.setattr(rtl, "find_flash_loader",
                        lambda tool_path=None, explicit=None: None)
    return path


# --------------------------------------------------------------------------- #
# Constants reflect the documented hardware facts
# --------------------------------------------------------------------------- #

def test_default_baud_is_1_5_mbaud():
    assert rtl.DEFAULT_BAUD == 1500000


def test_flash_base_and_offset():
    # Flash is memory-mapped at 0x08000000; the rtltool offset for "start of flash" is 0x0.
    assert rtl.FLASH_BASE == 0x08000000
    assert rtl.FLASH_BASE_OFFSET == "0x0"


def test_size_constants():
    assert rtl.SIZE_2MB == "0x200000"
    assert rtl.SIZE_4MB == "0x400000"
    assert rtl.DEFAULT_SIZE == rtl.SIZE_2MB


def test_flash_loader_name():
    assert rtl.FLASH_LOADER_NAME == "imgtool_flashloader_amebad.bin"


# --------------------------------------------------------------------------- #
# Tool discovery
# --------------------------------------------------------------------------- #

def test_find_rtltool_explicit_path(tmp_path):
    tool = tmp_path / "rtltool.py"
    tool.write_text("# fake tool\n", encoding="utf-8")
    found = rtl.find_rtltool(str(tool))
    assert found == os.path.abspath(str(tool))


def test_find_rtltool_explicit_missing_returns_none(tmp_path):
    assert rtl.find_rtltool(str(tmp_path / "nope.py")) is None


def test_find_rtltool_in_bundled_dir(tmp_path, monkeypatch):
    bundled = tmp_path / "tools"
    bundled.mkdir()
    tool = bundled / "rtltool.py"
    tool.write_text("# fake\n", encoding="utf-8")
    monkeypatch.setattr(rtl, "_bundled_tools_dirs", lambda: [str(bundled)])
    # ensure PATH lookup can't accidentally satisfy it
    monkeypatch.setattr(rtl.shutil, "which", lambda name: None)
    found = rtl.find_rtltool()
    assert found == os.path.abspath(str(tool))


def test_find_rtltool_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr(rtl, "_bundled_tools_dirs", lambda: [])
    monkeypatch.setattr(rtl.shutil, "which",
                        lambda name: "/usr/bin/rtltool" if name in rtl._TOOL_NAMES else None)
    found = rtl.find_rtltool()
    assert found == os.path.abspath("/usr/bin/rtltool")


def test_find_rtltool_not_found(monkeypatch):
    monkeypatch.setattr(rtl, "_bundled_tools_dirs", lambda: [])
    monkeypatch.setattr(rtl.shutil, "which", lambda name: None)
    assert rtl.find_rtltool() is None


def test_rtltool_available(monkeypatch):
    monkeypatch.setattr(rtl, "find_rtltool", lambda explicit=None: "/x/rtltool.py")
    assert rtl.rtltool_available() is True
    monkeypatch.setattr(rtl, "find_rtltool", lambda explicit=None: None)
    assert rtl.rtltool_available() is False


def test_find_flash_loader_next_to_tool(tmp_path):
    d = tmp_path / "tools"
    d.mkdir()
    tool = d / "rtltool.py"
    tool.write_text("# fake\n", encoding="utf-8")
    stub = d / rtl.FLASH_LOADER_NAME
    stub.write_bytes(b"\x00" * 16)
    found = rtl.find_flash_loader(str(tool))
    assert found == os.path.abspath(str(stub))


# --------------------------------------------------------------------------- #
# read_flash — the rf argv
# --------------------------------------------------------------------------- #

def test_read_flash_builds_rf_argv(monkeypatch, tmp_path):
    _force_tool(monkeypatch)
    fp = _install_fake_popen(monkeypatch, out_lines=["[100%] done"], returncode=0)
    out = str(tmp_path / "dump.bin")
    log = _Collector()

    rc = rtl.read_flash("COM7", out, size=rtl.SIZE_4MB, baud=rtl.DEFAULT_BAUD,
                        on_line=log, tool="/fake/rtltool.py")

    assert rc == 0
    argv = fp.last_argv
    # ".py" tool -> run with the interpreter
    assert argv[0] == rtl.sys.executable
    assert argv[1] == "/fake/rtltool.py"
    # -p <port> -b <baud>
    assert "-p" in argv and argv[argv.index("-p") + 1] == "COM7"
    assert "-b" in argv and argv[argv.index("-b") + 1] == str(rtl.DEFAULT_BAUD)
    # rf 0x0 <size> <out>  (read starts at flash base offset 0x0)
    assert argv[-4] == "rf"
    assert argv[-3] == rtl.FLASH_BASE_OFFSET == "0x0"
    assert argv[-2] == rtl.SIZE_4MB
    assert argv[-1] == out


def test_read_flash_streams_lines(monkeypatch, tmp_path):
    _force_tool(monkeypatch)
    _install_fake_popen(monkeypatch, out_lines=["alpha", "bravo", "charlie"], returncode=0)
    log = _Collector()
    rtl.read_flash("COM1", str(tmp_path / "d.bin"), on_line=log, tool="/fake/rtltool.py")
    assert "alpha" in log.lines and "bravo" in log.lines and "charlie" in log.lines


def test_read_flash_missing_tool_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(rtl, "find_rtltool", lambda explicit=None: None)
    with pytest.raises(rtl.RtlToolNotFound):
        rtl.read_flash("COM1", str(tmp_path / "d.bin"), on_line=_Collector())


def test_read_flash_emits_download_mode_help(monkeypatch, tmp_path):
    _force_tool(monkeypatch)
    _install_fake_popen(monkeypatch, out_lines=["ok"], returncode=0)
    log = _Collector()
    rtl.read_flash("COM1", str(tmp_path / "d.bin"), on_line=log, tool="/fake/rtltool.py")
    assert "download mode" in log.text.lower()


def test_read_flash_no_sync_annotation(monkeypatch, tmp_path):
    _force_tool(monkeypatch)
    _install_fake_popen(monkeypatch, out_lines=["Failed to connect: no response"],
                        returncode=1)
    log = _Collector()
    rc = rtl.read_flash("COM1", str(tmp_path / "d.bin"), on_line=log,
                        tool="/fake/rtltool.py")
    assert rc == 1
    assert "not in" in log.text.lower() and "download mode" in log.text.lower()


# --------------------------------------------------------------------------- #
# write_flash — the wf argv
# --------------------------------------------------------------------------- #

def _make_image(tmp_path, name="fw.bin", data=b"\xaa" * 64):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_write_flash_builds_wf_argv(monkeypatch, tmp_path):
    _force_tool(monkeypatch)
    fp = _install_fake_popen(monkeypatch, out_lines=["[100%]"], returncode=0)
    image = _make_image(tmp_path)
    log = _Collector()

    rc = rtl.write_flash("COM9", image, baud=rtl.DEFAULT_BAUD, on_line=log,
                         tool="/fake/rtltool.py")

    assert rc == 0
    argv = fp.last_argv
    assert "-p" in argv and argv[argv.index("-p") + 1] == "COM9"
    assert "-b" in argv and argv[argv.index("-b") + 1] == str(rtl.DEFAULT_BAUD)
    # wf <offset> <image>  (offset defaults to start-of-flash 0x0)
    assert argv[-3] == "wf"
    assert argv[-2] == rtl.FLASH_BASE_OFFSET == "0x0"
    assert argv[-1] == image


def test_write_flash_exe_tool_invoked_directly(monkeypatch, tmp_path):
    # an extension-less tool is invoked directly, NOT via the interpreter
    monkeypatch.setattr(rtl, "find_rtltool", lambda explicit=None: "/bin/rtltool")
    monkeypatch.setattr(rtl, "find_flash_loader",
                        lambda tool_path=None, explicit=None: None)
    fp = _install_fake_popen(monkeypatch, out_lines=["ok"], returncode=0)
    image = _make_image(tmp_path)
    rtl.write_flash("COM1", image, on_line=_Collector(), tool="/bin/rtltool")
    assert fp.last_argv[0] == "/bin/rtltool"
    assert rtl.sys.executable not in fp.last_argv


def test_write_flash_adds_flash_loader_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(rtl, "find_rtltool", lambda explicit=None: "/fake/rtltool.py")
    stub = str(tmp_path / rtl.FLASH_LOADER_NAME)
    (tmp_path / rtl.FLASH_LOADER_NAME).write_bytes(b"\x00" * 8)
    monkeypatch.setattr(rtl, "find_flash_loader",
                        lambda tool_path=None, explicit=None: stub)
    fp = _install_fake_popen(monkeypatch, out_lines=["ok"], returncode=0)
    image = _make_image(tmp_path)
    rtl.write_flash("COM1", image, on_line=_Collector(), tool="/fake/rtltool.py")
    argv = fp.last_argv
    assert "--flash-loader" in argv
    assert argv[argv.index("--flash-loader") + 1] == stub


def test_write_flash_no_flash_loader_flag_when_absent(monkeypatch, tmp_path):
    _force_tool(monkeypatch)  # find_flash_loader -> None
    fp = _install_fake_popen(monkeypatch, out_lines=["ok"], returncode=0)
    image = _make_image(tmp_path)
    rtl.write_flash("COM1", image, on_line=_Collector(), tool="/fake/rtltool.py")
    assert "--flash-loader" not in fp.last_argv


def test_write_flash_missing_image_raises_before_tool(monkeypatch):
    # Should raise on the missing image WITHOUT ever invoking the tool — guard the
    # destructive op against a bad path. We make Popen explode if called to prove it isn't.
    _force_tool(monkeypatch)

    def _boom(*a, **k):
        raise AssertionError("Popen must not be called when the image is missing")

    monkeypatch.setattr(rtl.subprocess, "Popen", _boom)
    with pytest.raises(FileNotFoundError):
        rtl.write_flash("COM1", "/no/such/image.bin", on_line=_Collector(),
                        tool="/fake/rtltool.py")


def test_write_flash_missing_tool_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(rtl, "find_rtltool", lambda explicit=None: None)
    image = _make_image(tmp_path)
    with pytest.raises(rtl.RtlToolNotFound):
        rtl.write_flash("COM1", image, on_line=_Collector())


def test_write_flash_unprotect_warning_surfaced(monkeypatch, tmp_path):
    _force_tool(monkeypatch)
    # rc==0 but output mentions unprotect -> the classic silent no-op gotcha.
    _install_fake_popen(monkeypatch,
                        out_lines=["flash unprotect failed", "done"], returncode=0)
    image = _make_image(tmp_path)
    log = _Collector()
    rtl.write_flash("COM1", image, on_line=log, tool="/fake/rtltool.py")
    assert "unprotect" in log.text.lower()


# --------------------------------------------------------------------------- #
# flash — DUMP-FIRST anti-brick orchestration
# --------------------------------------------------------------------------- #

def test_flash_dumps_before_write(monkeypatch, tmp_path):
    """flash() must call read_flash (dump) BEFORE write_flash."""
    _force_tool(monkeypatch)
    image = _make_image(tmp_path)
    order: list = []

    def fake_read(port, out, **kw):
        order.append("read")
        # simulate a real dump producing a non-empty file
        with open(out, "wb") as f:
            f.write(b"\x00" * 1024)
        return 0

    def fake_write(port, img, **kw):
        order.append("write")
        return 0

    monkeypatch.setattr(rtl, "read_flash", fake_read)
    monkeypatch.setattr(rtl, "write_flash", fake_write)

    rc = rtl.flash("COM1", image, on_line=_Collector(),
                   backup_dir=str(tmp_path / "bk"), tool="/fake/rtltool.py")
    assert rc == 0
    assert order == ["read", "write"], "dump must happen before the write"


def test_flash_writes_a_real_backup_file(monkeypatch, tmp_path):
    _force_tool(monkeypatch)
    image = _make_image(tmp_path)
    backup_dir = tmp_path / "bk"

    def fake_read(port, out, **kw):
        with open(out, "wb") as f:
            f.write(b"\x11" * 2048)
        return 0

    monkeypatch.setattr(rtl, "read_flash", fake_read)
    monkeypatch.setattr(rtl, "write_flash", lambda *a, **k: 0)

    rtl.flash("COM1", image, on_line=_Collector(), backup_dir=str(backup_dir),
              tool="/fake/rtltool.py")
    dumps = list(backup_dir.glob("rtl8720_backup_*.bin"))
    assert len(dumps) == 1
    assert dumps[0].stat().st_size == 2048


def test_flash_aborts_when_backup_fails(monkeypatch, tmp_path):
    """If the pre-write dump fails, flash() must NOT write."""
    _force_tool(monkeypatch)
    image = _make_image(tmp_path)
    wrote = {"called": False}

    monkeypatch.setattr(rtl, "read_flash", lambda port, out, **kw: 1)  # dump fails

    def fake_write(*a, **k):
        wrote["called"] = True
        return 0

    monkeypatch.setattr(rtl, "write_flash", fake_write)

    log = _Collector()
    rc = rtl.flash("COM1", image, on_line=log, backup_dir=str(tmp_path / "bk"),
                   tool="/fake/rtltool.py")
    assert rc != 0
    assert wrote["called"] is False, "must not write when the backup failed"
    assert "refusing to write" in log.text.lower()


def test_flash_aborts_when_backup_empty(monkeypatch, tmp_path):
    """A 0-byte 'backup' is not a real backup; flash() must refuse to write."""
    _force_tool(monkeypatch)
    image = _make_image(tmp_path)
    wrote = {"called": False}

    def fake_read(port, out, **kw):
        # create the file but leave it empty
        open(out, "wb").close()
        return 0

    monkeypatch.setattr(rtl, "read_flash", fake_read)

    def fake_write(*a, **k):
        wrote["called"] = True
        return 0

    monkeypatch.setattr(rtl, "write_flash", fake_write)

    rc = rtl.flash("COM1", image, on_line=_Collector(), backup_dir=str(tmp_path / "bk"),
                   tool="/fake/rtltool.py")
    assert rc != 0
    assert wrote["called"] is False


def test_flash_skip_backup_does_not_dump(monkeypatch, tmp_path):
    _force_tool(monkeypatch)
    image = _make_image(tmp_path)
    read_called = {"n": 0}
    write_called = {"n": 0}

    def fake_read(*a, **k):
        read_called["n"] += 1
        return 0

    def fake_write(*a, **k):
        write_called["n"] += 1
        return 0

    monkeypatch.setattr(rtl, "read_flash", fake_read)
    monkeypatch.setattr(rtl, "write_flash", fake_write)

    rc = rtl.flash("COM1", image, on_line=_Collector(), skip_backup=True,
                   tool="/fake/rtltool.py")
    assert rc == 0
    assert read_called["n"] == 0, "skip_backup must skip the dump"
    assert write_called["n"] == 1


def test_flash_missing_image_raises(monkeypatch):
    _force_tool(monkeypatch)
    with pytest.raises(FileNotFoundError):
        rtl.flash("COM1", "/no/such.bin", on_line=_Collector(), tool="/fake/rtltool.py")


def test_flash_missing_tool_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(rtl, "find_rtltool", lambda explicit=None: None)
    image = _make_image(tmp_path)
    with pytest.raises(rtl.RtlToolNotFound):
        rtl.flash("COM1", image, on_line=_Collector())


def test_flash_propagates_write_failure(monkeypatch, tmp_path):
    _force_tool(monkeypatch)
    image = _make_image(tmp_path)

    def fake_read(port, out, **kw):
        with open(out, "wb") as f:
            f.write(b"\x00" * 512)
        return 0

    monkeypatch.setattr(rtl, "read_flash", fake_read)
    monkeypatch.setattr(rtl, "write_flash", lambda *a, **k: 5)  # write fails

    rc = rtl.flash("COM1", image, on_line=_Collector(), backup_dir=str(tmp_path / "bk"),
                   tool="/fake/rtltool.py")
    assert rc == 5


# --------------------------------------------------------------------------- #
# Error classes / guidance
# --------------------------------------------------------------------------- #

def test_rtltoolnotfound_is_filenotfound_with_guidance():
    err = rtl.RtlToolNotFound()
    assert isinstance(err, FileNotFoundError)
    msg = str(err).lower()
    assert "rtltool" in msg
    # guidance must steer the user away from the wrong tools
    assert "esptool" in msg or "ltchiptool" in msg


def test_install_guidance_mentions_amebad():
    assert "amebad" in rtl.install_guidance().lower()


def test_download_mode_help_mentions_pin_sequence():
    help_text = rtl.download_mode_help().lower()
    assert "download mode" in help_text
    assert "pa7" in help_text or "reset" in help_text


# --------------------------------------------------------------------------- #
# AmebaD ImageTool path (flash_ambd) — the hardware-proven flash method
# --------------------------------------------------------------------------- #

def _make_bundle(tmp_path):
    """Create a complete AmebaD bundle dir (3 images + SRAM loader) under tmp_path."""
    d = tmp_path / "bundle"
    d.mkdir()
    for n in rtl.AMBD_BUNDLE_FILES:
        (d / n).write_bytes(b"\x00" * 16)
    (d / rtl.FLASH_LOADER_NAME).write_bytes(b"\x00" * 16)
    return str(d)


def test_find_ambd_tool_explicit(tmp_path):
    exe = tmp_path / "upload_image_tool_linux"
    exe.write_text("#!/bin/sh\n")
    assert rtl.find_ambd_tool(str(exe)) == os.path.abspath(str(exe))
    assert rtl.find_ambd_tool(str(tmp_path / "nope")) is None


def test_find_ambd_tool_env(tmp_path, monkeypatch):
    exe = tmp_path / "upload_image_tool_linux"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setenv("CYBERC_AMEBAD_TOOL", str(exe))
    monkeypatch.setattr(rtl, "_realtek_tool_dirs", lambda: [])
    monkeypatch.setattr(rtl.shutil, "which", lambda name: None)
    assert rtl.find_ambd_tool() == os.path.abspath(str(exe))


def test_ambd_tool_available_false(monkeypatch):
    monkeypatch.setattr(rtl, "find_ambd_tool", lambda explicit=None: None)
    assert rtl.ambd_tool_available() is False


def test_flash_ambd_missing_tool_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(rtl, "find_ambd_tool", lambda explicit=None: None)
    with pytest.raises(rtl.RtlToolNotFound):
        rtl.flash_ambd("COM8", _make_bundle(tmp_path))


def test_flash_ambd_incomplete_bundle_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(rtl, "find_ambd_tool",
                        lambda explicit=None: "/fake/upload_image_tool_linux")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        rtl.flash_ambd("COM8", str(empty))


def test_flash_ambd_success_argv_and_detection(tmp_path, monkeypatch):
    tool = "/fake/upload_image_tool_linux"
    monkeypatch.setattr(rtl, "find_ambd_tool", lambda explicit=None: tool)
    fp = _install_fake_popen(monkeypatch, out_lines=[
        "set baudrate to 115200.", "enter download flash mode.",
        "app has been sent successfully.",
        "verifying km0 km4 and app blocks....ok.", "done.",
    ], returncode=0)
    bundle = _make_bundle(tmp_path)
    out = _Collector()
    rc = rtl.flash_ambd("COM8", bundle, on_line=out)
    assert rc == 0
    argv = fp.last_argv
    assert argv[0] == tool and argv[1] == bundle and argv[2] == "COM8"
    assert "--auto=1" in argv
    assert any("verified" in l.lower() for l in out.lines)


def test_flash_ambd_failure_detected_from_output(tmp_path, monkeypatch):
    # The ImageTool exits 0 even on failure — success/failure is judged from output.
    monkeypatch.setattr(rtl, "find_ambd_tool",
                        lambda explicit=None: "/fake/upload_image_tool_linux")
    _install_fake_popen(monkeypatch,
                        out_lines=["enter download flash mode.", "** sync timeout.", "failed."],
                        returncode=0)
    rc = rtl.flash_ambd("COM8", _make_bundle(tmp_path), on_line=_Collector())
    assert rc != 0


def test_flash_ambd_auto_false_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(rtl, "find_ambd_tool",
                        lambda explicit=None: "/fake/upload_image_tool_linux")
    fp = _install_fake_popen(monkeypatch, out_lines=["done."], returncode=0)
    rtl.flash_ambd("COM8", _make_bundle(tmp_path), auto=False, on_line=_Collector())
    assert "--auto=0" in fp.last_argv


def test_flash_ambd_success_with_sync_chatter_is_not_a_failure(tmp_path, monkeypatch):
    # Ledger C-1 regression: the ImageTool prints normal "sync" chatter during a SUCCESSFUL flash. A bare
    # "sync" substring used to flip success -> no-sync failure; now success ("done." + no "failed") wins.
    monkeypatch.setattr(rtl, "find_ambd_tool",
                        lambda explicit=None: "/fake/upload_image_tool_linux")
    _install_fake_popen(monkeypatch, out_lines=[
        "enter download flash mode.", "syncing with rom loader...", "sync ok.",
        "app has been sent successfully.", "verifying km0 km4 and app blocks....ok.", "done.",
    ], returncode=0)
    out = _Collector()
    rc = rtl.flash_ambd("COM8", _make_bundle(tmp_path), on_line=out)
    assert rc == 0
    assert not any("no ROM-loader sync" in l for l in out.lines)  # not annotated as a no-sync failure


def test_looks_like_no_sync_ignores_benign_syncing():
    # Benign "syncing"/"sync ok" chatter must NOT read as a no-sync failure (C-1)...
    assert rtl._looks_like_no_sync("syncing with rom loader... sync ok. done.") is False
    # ...but genuine failure phrasings still do.
    assert rtl._looks_like_no_sync("** sync timed out") is True
    assert rtl._looks_like_no_sync("failed to sync with target") is True


def test_flash_ambd_missing_tool_raises_precise_ambd_type(tmp_path, monkeypatch):
    # The ImageTool-missing error is now AmbdToolNotFound — still a RtlToolNotFound for back-compat.
    monkeypatch.setattr(rtl, "find_ambd_tool", lambda explicit=None: None)
    with pytest.raises(rtl.AmbdToolNotFound):
        rtl.flash_ambd("COM8", _make_bundle(tmp_path))
    assert issubclass(rtl.AmbdToolNotFound, rtl.RtlToolNotFound)


def test_flash_ambd_done_plus_real_failure_is_still_failure(tmp_path, monkeypatch):
    # The load-bearing invariant of the C-1 fix: a genuine no-sync failure must WIN even when "done." also
    # appears (a partial step can print "done." before the flash times out) — success is never inferred.
    monkeypatch.setattr(rtl, "find_ambd_tool",
                        lambda explicit=None: "/fake/upload_image_tool_linux")
    _install_fake_popen(monkeypatch, out_lines=["km0 done.", "sync timed out"], returncode=0)
    rc = rtl.flash_ambd("COM8", _make_bundle(tmp_path), on_line=_Collector())
    assert rc != 0
