"""The self-update apply path refuses hosts without a published build instead of coercing them to
another architecture's asset.

Before this gate, ``self_update.platform_key`` mapped every Windows machine to windows-x64, every
macOS machine to macos-arm64 and every non-arm64 Linux machine to linux-x64, so a frozen build on
32-bit or ARM64 Windows, an Intel Mac, or 32-bit ARM Linux downloaded the wrong-architecture asset,
verified its (correct) checksum, staged it and swapped it in. The strict selector in
``update_select`` refuses exactly those hosts; the apply path now goes through it.

Doubles only: no network, no real binary, no swap. The release, checksum fetch and download are
replaced; the download double fails the test if it is ever reached on a refused host.
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from src.core import self_update as su
from src.core import update_select as us
from tests import exe_images as img

TAG = "v9.9.9"
NAMES = [
    f"cyber-controller-{TAG}-windows-x64.exe",
    f"cyber-controller-{TAG}-windows-x64-setup.exe",
    f"cyber-controller-{TAG}-linux-x64",
    f"cyber-controller-{TAG}-linux-arm64",
    f"cyber-controller-{TAG}-macos-arm64",
]
# Structurally valid images per asset (the staging header gate is real, not bypassed).
CONTENT = {name: img.image_for(next(k for k in img.IMAGE_FOR_KEY if k in name)) for name in NAMES}
BASE = f"https://github.com/LxveAce/cyber-controller/releases/download/{TAG}/"

SUPPORTED = [
    ("Windows", "AMD64", "windows-x64"),
    ("Windows", "x86_64", "windows-x64"),
    ("win32", "x64", "windows-x64"),
    ("Darwin", "arm64", "macos-arm64"),
    ("Darwin", "aarch64", "macos-arm64"),
    ("Linux", "x86_64", "linux-x64"),
    ("Linux", "amd64", "linux-x64"),
    ("Linux", "aarch64", "linux-arm64"),
    ("Linux", "arm64", "linux-arm64"),
]
REFUSED = [
    ("Windows", "x86"),
    ("Windows", "ARM64"),
    ("Windows", "aarch64"),
    ("Darwin", "x86_64"),
    ("Linux", "armv7l"),
    ("Linux", "riscv64"),
    ("Linux", "i686"),
    ("FreeBSD", "amd64"),
]


def release():
    assets = [{"name": n, "browser_download_url": BASE + n, "size": len(CONTENT[n])} for n in NAMES]
    assets.append({"name": "SHA256SUMS.txt", "browser_download_url": BASE + "SHA256SUMS.txt"})
    return {"tag_name": TAG, "draft": False, "prerelease": False, "assets": assets}


@pytest.fixture
def frozen_onefile(monkeypatch, tmp_path):
    exe = tmp_path / "cyber-controller"
    exe.write_bytes(b"current build (double)\n")
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    monkeypatch.setattr(su, "installed_kind", lambda: "onefile")
    monkeypatch.setattr(su, "current_exe", lambda: str(exe))
    monkeypatch.setattr(su, "fetch_sums", lambda assets, timeout=0: {
        name: hashlib.sha256(data).hexdigest() for name, data in CONTENT.items()})
    return exe


def host(monkeypatch, system, machine):
    monkeypatch.setattr(su.platform, "system", lambda: system)
    monkeypatch.setattr(su.platform, "machine", lambda: machine)


@pytest.mark.parametrize("system, machine, key", SUPPORTED)
def test_published_shapes_resolve_to_the_same_key_as_the_strict_selector(system, machine, key):
    assert su.platform_key(system, machine) == key
    assert us.supported_platform_key(system, machine) == key


@pytest.mark.parametrize("system, machine", REFUSED)
def test_hosts_without_a_published_build_are_refused_not_coerced(system, machine):
    with pytest.raises(su.SelfUpdateError) as info:
        su.platform_key(system, machine)
    message = str(info.value)
    assert "no published build for this machine" in message
    assert system in message and machine in message, "names the host so the user can act"
    assert "release page" in message
    assert isinstance(info.value.__cause__, us.UnsupportedPlatform), "chained to the strict refusal"


@pytest.mark.parametrize("system, machine", REFUSED)
def test_self_update_refuses_before_any_download_on_a_refused_host(monkeypatch, frozen_onefile,
                                                                   system, machine):
    host(monkeypatch, system, machine)
    monkeypatch.setattr(su, "download_asset",
                        lambda *a, **k: pytest.fail("download_asset reached on a refused host"))
    with pytest.raises(su.SelfUpdateError, match="no published build"):
        su.self_update(SimpleNamespace(latest_tag=TAG), releases=[release()], restart=False)
    assert not list(frozen_onefile.parent.glob("*.part")) and not list(
        frozen_onefile.parent.glob("*.new")), "nothing staged next to the running binary"


@pytest.mark.parametrize("system, machine, key", [
    ("Linux", "x86_64", "linux-x64"), ("Linux", "aarch64", "linux-arm64"),
    ("Darwin", "arm64", "macos-arm64"),
])
def test_published_host_still_stages_its_own_asset(monkeypatch, frozen_onefile, system, machine,
                                                   key):
    host(monkeypatch, system, machine)

    def download(url, dest, timeout=0, progress=None):
        with open(dest, "wb") as fh:
            fh.write(CONTENT[url.rsplit("/", 1)[1]])

    monkeypatch.setattr(su, "download_asset", download)
    staged = su.self_update(SimpleNamespace(latest_tag=TAG), releases=[release()], restart=False)
    assert staged.endswith(f"cyber-controller-{TAG}-{key}.new")


def test_default_arguments_read_the_running_platform(monkeypatch):
    host(monkeypatch, "Linux", "armv7l")
    with pytest.raises(su.SelfUpdateError, match="Linux/armv7l"):
        su.platform_key()
    host(monkeypatch, "Linux", "aarch64")
    assert su.platform_key() == "linux-arm64"
