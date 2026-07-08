"""Characterization tests for src/core/backends/adb_backend.py pure logic.

Covers the (github-only) host allowlist, filename guard, release-asset selection by OS/arch
(_pick_platform_asset), and the version-compare in check_version. Subprocess/network calls are monkeypatched.
"""

import sys
import threading
import time

import pytest

adb = pytest.importorskip("src.core.backends.adb_backend")


# ── _host_allowed (narrower than sd_backend — github only, no kali) ────────
@pytest.mark.parametrize("host,ok", [
    ("github.com", True),
    ("api.github.com", True),
    ("x.githubusercontent.com", True),
    ("kali.download", False),        # allowed for sd_backend, NOT for adb
    ("foo.kali.download", False),
    ("evil.com", False),
    (None, False),
])
def test_host_allowed(host, ok):
    assert adb._host_allowed(host) is ok


def test_safe_filename_accepts_and_rejects():
    assert adb._safe_filename("rayhunter.zip") == "rayhunter.zip"
    with pytest.raises(ValueError):
        adb._safe_filename("../rayhunter.zip")


# ── _pick_platform_asset ──────────────────────────────────────────────────
def _set_platform(monkeypatch, system, machine):
    monkeypatch.setattr(adb.platform, "system", lambda: system)
    monkeypatch.setattr(adb.platform, "machine", lambda: machine)


def test_pick_asset_matches_os_and_arch(monkeypatch):
    _set_platform(monkeypatch, "Linux", "x86_64")
    assets = [{"name": "rayhunter-linux-x86_64.zip", "browser_download_url": "u1"}]
    assert adb._pick_platform_asset(assets)["browser_download_url"] == "u1"


def test_pick_asset_os_mismatch_returns_none(monkeypatch):
    _set_platform(monkeypatch, "Windows", "x86_64")
    assets = [{"name": "rayhunter-linux-x86_64.zip", "browser_download_url": "u1"}]
    assert adb._pick_platform_asset(assets) is None


def test_pick_asset_prefers_more_specific_arch(monkeypatch):
    _set_platform(monkeypatch, "Linux", "arm64")
    assets = [
        {"name": "app-linux-arm.zip", "browser_download_url": "arm"},
        {"name": "app-linux-aarch64.zip", "browser_download_url": "aarch64"},
    ]
    # arch order for arm64 is [aarch64, arm64, arm] -> aarch64 scores highest.
    assert adb._pick_platform_asset(assets)["browser_download_url"] == "aarch64"


def test_pick_asset_ignores_non_zip(monkeypatch):
    _set_platform(monkeypatch, "Linux", "x86_64")
    assert adb._pick_platform_asset([{"name": "notes-linux-x86_64.txt"}]) is None


def test_pick_asset_empty_list(monkeypatch):
    _set_platform(monkeypatch, "Linux", "x86_64")
    assert adb._pick_platform_asset([]) is None


# Real RayHunter v0.11.2 asset naming — macOS assets are "macos-intel"/"macos-arm", NOT x86_64/arm64.
_RAYHUNTER_ASSETS = [
    {"name": "rayhunter-v0.11.2-linux-aarch64.zip", "browser_download_url": "linux-aarch64"},
    {"name": "rayhunter-v0.11.2-linux-armv7.zip", "browser_download_url": "linux-armv7"},
    {"name": "rayhunter-v0.11.2-linux-x64.zip", "browser_download_url": "linux-x64"},
    {"name": "rayhunter-v0.11.2-macos-arm.zip", "browser_download_url": "macos-arm"},
    {"name": "rayhunter-v0.11.2-macos-intel.zip", "browser_download_url": "macos-intel"},
    {"name": "rayhunter-v0.11.2-windows-x86_64.zip", "browser_download_url": "windows-x86_64"},
]


def test_pick_asset_intel_mac_gets_macos_intel(monkeypatch):
    # REGRESSION (BUGHUNT-0708 #4): an Intel Mac reports machine=x86_64, but the asset is named
    # "macos-intel" — before the fix this matched nothing and Intel Macs got no firmware.
    _set_platform(monkeypatch, "Darwin", "x86_64")
    got = adb._pick_platform_asset(_RAYHUNTER_ASSETS)
    assert got is not None and got["browser_download_url"] == "macos-intel"


def test_pick_asset_apple_silicon_gets_macos_arm(monkeypatch):
    _set_platform(monkeypatch, "Darwin", "arm64")
    got = adb._pick_platform_asset(_RAYHUNTER_ASSETS)
    assert got is not None and got["browser_download_url"] == "macos-arm"


def test_pick_asset_windows_x64_gets_windows(monkeypatch):
    _set_platform(monkeypatch, "Windows", "AMD64")  # platform.machine() -> "AMD64" on Windows
    got = adb._pick_platform_asset(_RAYHUNTER_ASSETS)
    assert got is not None and got["browser_download_url"] == "windows-x86_64"


def test_pick_asset_linux_x64_gets_linux_x64(monkeypatch):
    _set_platform(monkeypatch, "Linux", "x86_64")
    got = adb._pick_platform_asset(_RAYHUNTER_ASSETS)
    assert got is not None and got["browser_download_url"] == "linux-x64"


# ── check_version compare (installed vs latest) ────────────────────────────
def _patch_versions(monkeypatch, installed, latest_tag):
    monkeypatch.setattr(adb, "installed_version", lambda *a, **k: installed)
    monkeypatch.setattr(adb, "latest_version", lambda *a, **k: (latest_tag, "url"))


def test_check_version_equal_no_update(monkeypatch):
    _patch_versions(monkeypatch, "v1.2", "1.2")  # leading 'v' stripped on both sides -> equal
    assert adb.check_version(lambda _l: None)["update_available"] is False


def test_check_version_differ_update(monkeypatch):
    _patch_versions(monkeypatch, "1.0", "2.0")
    assert adb.check_version(lambda _l: None)["update_available"] is True


def test_check_version_no_installed(monkeypatch):
    _patch_versions(monkeypatch, None, "2.0")
    assert adb.check_version(lambda _l: None)["update_available"] is False


def test_check_version_no_latest(monkeypatch):
    _patch_versions(monkeypatch, "1.0", None)
    assert adb.check_version(lambda _l: None)["update_available"] is False


# ── latest_version unknown profile (pure) + registry drift-lock ────────────
def test_latest_version_unknown_profile():
    assert adb.latest_version("does-not-exist") == (None, None)


def test_adb_profiles_has_rayhunter():
    assert adb.ADB_PROFILES["rayhunter"]["repo"] == "EFForg/rayhunter"


# ── install_manual: partial-push failures must NOT report success ──────────
def _stub_manual_env(monkeypatch, tmp_path, push, config_exists=True, probe_reply=None):
    """Wire install_manual with a real daemon file, a canned adb_shell, and *push*.

    adb_shell answers the 'test -f config' probe with EXISTS/MISSING so we can steer
    whether the config push is attempted; all other shell calls succeed (rc 0).
    Pass ``probe_reply=(rc, output)`` to override that probe answer directly (e.g. to
    simulate a transiently failed probe that reports neither token).
    """
    daemon = tmp_path / "rayhunter-daemon"
    daemon.write_bytes(b"\x7fELF fake daemon")

    marker = "EXISTS" if config_exists else "MISSING"

    def fake_shell(command, on_line, serial=None):
        if "test -f" in command:
            return probe_reply if probe_reply is not None else (0, marker)
        return (0, "")

    monkeypatch.setattr(adb, "adb_shell", fake_shell)
    monkeypatch.setattr(adb, "adb_push", push)
    return str(daemon)


def test_install_manual_fails_when_init_push_fails(monkeypatch, tmp_path):
    # The daemon push succeeds but the init-script push fails. The init script is what
    # auto-starts the daemon at boot, so this is a broken install and must NOT return 0.
    def push(local, remote, on_line, serial=None):
        return 7 if remote == adb._DEVICE_INIT else 0

    daemon = _stub_manual_env(monkeypatch, tmp_path, push, config_exists=True)
    log = []
    rc = adb.install_manual(daemon, log.append, serial="X")
    assert rc == 7, "a failed init-script push must propagate a non-zero rc"
    assert "complete" not in "\n".join(log).lower(), "must not claim success"


def test_install_manual_fails_when_config_push_fails(monkeypatch, tmp_path):
    # Config missing on device -> config push attempted and fails; must propagate.
    def push(local, remote, on_line, serial=None):
        return 4 if remote == adb._DEVICE_CONFIG else 0

    daemon = _stub_manual_env(monkeypatch, tmp_path, push, config_exists=False)
    log = []
    rc = adb.install_manual(daemon, log.append, serial="X")
    assert rc == 4
    assert "complete" not in "\n".join(log).lower()


def test_install_manual_success_when_all_pushes_ok(monkeypatch, tmp_path):
    # Positive control: every push succeeds -> rc 0 and the completion message is logged.
    daemon = _stub_manual_env(monkeypatch, tmp_path,
                              lambda l, r, on_line, serial=None: 0, config_exists=False)
    log = []
    rc = adb.install_manual(daemon, log.append, serial="X")
    assert rc == 0
    assert "complete" in "\n".join(log).lower()


def test_install_manual_pushes_config_when_probe_indeterminate(monkeypatch, tmp_path):
    # The 'test -f config' probe transiently fails (rc != 0, "error: device offline") so its
    # output carries neither EXISTS nor MISSING — common while the transport re-settles right
    # after the big binary push. The config state is UNKNOWN, so the install must push the
    # default config rather than assume it exists. Pre-fix, the missing "MISSING" token routed
    # control into the 'skip' branch and the config was never pushed.
    pushed = []

    def push(local, remote, on_line, serial=None):
        pushed.append(remote)
        return 0

    daemon = _stub_manual_env(monkeypatch, tmp_path, push,
                              probe_reply=(1, "error: device offline"))
    log = []
    rc = adb.install_manual(daemon, log.append, serial="X")
    assert rc == 0
    assert adb._DEVICE_CONFIG in pushed, "an indeterminate probe must push the config, not skip it"
    assert "skipping" not in "\n".join(log).lower()


def test_install_manual_indeterminate_probe_config_push_failure_propagates(monkeypatch, tmp_path):
    # Same indeterminate probe, but the config push then fails. Pre-fix the probe was mis-read
    # as 'exists', the config push was skipped, and install_manual returned 0 — a broken install
    # (no config.toml, daemon can't start) reported as success. Now the config is pushed, the
    # failure surfaces, and rc is non-zero.
    def push(local, remote, on_line, serial=None):
        return 4 if remote == adb._DEVICE_CONFIG else 0

    daemon = _stub_manual_env(monkeypatch, tmp_path, push,
                              probe_reply=(1, "error: device offline"))
    log = []
    rc = adb.install_manual(daemon, log.append, serial="X")
    assert rc == 4, "a failed config push after an indeterminate probe must not report success"
    assert "complete" not in "\n".join(log).lower()


# ── _run_adb: the wall-clock timeout must be enforced on a silent child ─────
def test_run_adb_enforces_timeout_on_silent_child():
    """A child that emits no stdout and does not exit must still honour the timeout.

    Regression for the wait_for_device hang. `_run_adb` read stdout with a blocking
    `for line in proc.stdout` loop and only called proc.wait(timeout=...) AFTER that
    loop drained to EOF. A silent, long-running child (like `adb wait-for-device`
    with nothing attached) never produces EOF, so the timeout was never reached: the
    call blocked until the child exited on its own and the adb process was leaked.
    Post-fix the read happens on a reader thread, so proc.wait(timeout=...) fires,
    the child is killed, and the call returns ~timeout with rc == -1.
    """
    # Silent child: prints nothing, sleeps far longer than the _run_adb timeout.
    args = [sys.executable, "-c", "import time; time.sleep(30)"]
    log = []
    result = {}

    def run():
        result["rc"], result["out"] = adb._run_adb(args, log.append, timeout=1)

    t = threading.Thread(target=run, daemon=True)
    start = time.monotonic()
    t.start()
    t.join(timeout=10)
    elapsed = time.monotonic() - start

    assert not t.is_alive(), (
        "_run_adb blocked past its timeout on a silent child — the read loop "
        "never reached proc.wait(timeout=...)"
    )
    assert elapsed < 10, "should return near the 1s timeout, not the child's 30s runtime"
    assert result["rc"] == -1, "the timeout path must return rc -1"
    assert any("timed out" in line.lower() for line in log), "must log a timeout"


# ── secret redaction: admin password must never reach the on_line log sink ──
def test_redacted_cmdline_masks_admin_password():
    # RayHunter's network installer takes the device admin password on its argv.
    # That argv is echoed to on_line (the UI/console log, which can be exported),
    # so the password value must be masked in the display string.
    args = ["/tmp/installer", "orbic", "--admin-password", "hunter2",
            "--admin-ip", "192.168.1.1"]
    line = adb._redacted_cmdline(args)
    assert "hunter2" not in line, "cleartext admin password must not appear in the log line"
    assert "--admin-password ***" in line, "the secret value must be masked"
    # non-secret tokens are left intact
    assert "orbic" in line
    assert "--admin-ip 192.168.1.1" in line


def test_redacted_cmdline_masks_trailing_password_value():
    # Password as the last token (no trailing flags) must still be masked.
    args = ["installer", "orbic", "--admin-password", "s3cr3t"]
    assert adb._redacted_cmdline(args) == "$ installer orbic --admin-password ***"


def test_run_adb_does_not_log_admin_password_end_to_end():
    # _run_adb echoes the argv to on_line as its very first action. Feed it an argv
    # shaped like the RayHunter network installer and assert the plaintext password
    # never reaches the log sink. A trivial python child stands in for the installer
    # so nothing real runs; it exits 0 immediately.
    args = [sys.executable, "-c", "pass", "--admin-password", "hunter2"]
    log = []
    adb._run_adb(args, log.append, timeout=10)
    joined = "\n".join(log)
    assert "hunter2" not in joined, "plaintext admin password leaked to the on_line log sink"
    assert "--admin-password ***" in joined, "the echoed argv must mask the password value"
