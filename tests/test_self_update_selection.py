"""The operational updater pins exactly one published release, one canonical asset, one checksum
manifest and one published size before it creates any file, through the strict selector in
``update_select``; the permissive helpers it replaced are gone.

Doubles only: the GitHub transport is a fake opener serving bytes from a dict, temp files stand in
for the running binary, and the swap never runs. Nothing is downloaded, executed or installed.
"""
from __future__ import annotations

import hashlib
import os
from types import SimpleNamespace

import pytest

from src.core import self_update as su
from src.core import update_select as us
from tests import exe_images as img

TAG = "v9.9.9"
BASE = "https://github.com/LxveAce/cyber-controller/releases/download/" + TAG + "/"
SUMS_URL = BASE + "SHA256SUMS.txt"
HOSTS = {"linux-x64": ("Linux", "x86_64"), "linux-arm64": ("Linux", "aarch64"),
         "windows-x64": ("Windows", "AMD64"), "macos-arm64": ("Darwin", "arm64")}


def canonical(key, tag=TAG):
    return us.expected_asset_name(tag, key)


def sums_text(entries):
    return "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in entries)


class FakeResponse:
    """A urllib-like response over bytes: read(n) honours n; headers are whatever the test says."""

    def __init__(self, data, headers=None):
        self.data, self.pos, self.headers = data, 0, headers or {}
        self.requested, self.served = [], 0

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self.data) - self.pos
        chunk = self.data[self.pos:self.pos + n]
        self.pos += len(chunk)
        self.requested.append(n)
        self.served += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def transport(monkeypatch, payloads, headers=None):
    """Serve *payloads* (url -> bytes) through su._open; returns (opened urls, responses)."""
    opened, responses = [], []

    def fake_open(url, timeout):
        opened.append(url)
        if url not in payloads:
            raise OSError(f"no such url {url}")
        resp = FakeResponse(payloads[url], headers)
        responses.append(resp)
        return resp

    monkeypatch.setattr(su, "_open", fake_open)
    return opened, responses


@pytest.fixture
def frozen_onefile(monkeypatch, tmp_path):
    exe = tmp_path / "cyber-controller"
    exe.write_bytes(b"current build (double)")
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    monkeypatch.setattr(su, "installed_kind", lambda: "onefile")
    monkeypatch.setattr(su, "current_exe", lambda: str(exe))
    return exe


def host(monkeypatch, key):
    system, machine = HOSTS[key]
    monkeypatch.setattr(su.platform, "system", lambda: system)
    monkeypatch.setattr(su.platform, "machine", lambda: machine)


def asset(name, data, **extra):
    entry = {"name": name, "browser_download_url": BASE + name, "size": len(data)}
    entry.update(extra)
    return entry


def sums_asset():
    return {"name": "SHA256SUMS.txt", "browser_download_url": SUMS_URL}


def release(assets, tag=TAG, **flags):
    entry = {"tag_name": tag, "draft": False, "prerelease": False, "assets": assets}
    entry.update(flags)
    return entry


def catalog(key, data, extra_assets=(), manifest=None):
    """One release: the canonical binary for *key*, SHA256SUMS.txt and *extra_assets*; returns
    (releases, payloads)."""
    name = canonical(key)
    manifest = sums_text([(name, data)]).encode() if manifest is None else manifest
    assets = [asset(name, data), sums_asset(), *extra_assets]
    return [release(assets)], {BASE + name: data, SUMS_URL: manifest}


def no_download(monkeypatch):
    monkeypatch.setattr(su, "download_asset",
                        lambda *a, **k: pytest.fail("download_asset reached before a refusal"))


def no_network(monkeypatch):
    monkeypatch.setattr(su, "_open",
                        lambda *a, **k: pytest.fail("network reached before a refusal"))


def run(releases, key="linux-x64", tag=TAG, progress=None):
    return su.self_update(SimpleNamespace(latest_tag=tag), releases=releases, restart=False,
                          progress=progress)


def nothing_staged(directory):
    assert not list(directory.glob("*.part")) and not list(directory.glob("*.new"))


def x64():
    return img.image_for("linux-x64")


# ---- the whole operational chain over a fake transport -----------------------------------------

@pytest.mark.parametrize("key", list(HOSTS))
def test_canonical_catalog_is_staged_through_the_strict_chain(monkeypatch, frozen_onefile, key):
    host(monkeypatch, key)
    data = img.image_for(key)
    releases, payloads = catalog(key, data)
    opened, _ = transport(monkeypatch, payloads, headers={})          # no Content-Length at all
    seen = []
    staged = run(releases, key, progress=lambda d, t: seen.append((d, t)))
    assert staged.endswith(canonical(key) + ".new")
    with open(staged, "rb") as fh:
        assert fh.read() == data
    assert opened == [SUMS_URL, BASE + canonical(key)], "the manifest first, then the one asset"
    assert seen and seen[-1] == (len(data), len(data)), "progress against the published size"
    assert not list(frozen_onefile.parent.glob("*.part"))


def test_the_real_catalog_shape_selects_the_portable_never_the_installer(monkeypatch,
                                                                        frozen_onefile):
    host(monkeypatch, "windows-x64")
    data = img.image_for("windows-x64")
    installer = asset(canonical("windows-x64").replace(".exe", "-setup.exe"), b"installer bytes")
    others = [asset(canonical(k), img.image_for(k))
              for k in ("linux-x64", "linux-arm64", "macos-arm64")]
    releases, payloads = catalog("windows-x64", data, extra_assets=[installer, *others])
    transport(monkeypatch, payloads)
    assert run(releases, "windows-x64").endswith(canonical("windows-x64") + ".new")


def test_malformed_content_length_does_not_defeat_a_valid_download(monkeypatch, frozen_onefile):
    host(monkeypatch, "linux-x64")
    releases, payloads = catalog("linux-x64", x64())
    transport(monkeypatch, payloads, headers={"Content-Length": "not a number"})
    assert run(releases).endswith(".new")


def test_one_stable_entry_with_the_exact_tag_is_selected(monkeypatch, frozen_onefile):
    host(monkeypatch, "linux-x64")
    releases, payloads = catalog("linux-x64", x64())
    releases.append(release([asset(canonical("linux-x64", "v9.9.8"), b"old")], tag="v9.9.8"))
    transport(monkeypatch, payloads)
    assert run(releases).endswith(canonical("linux-x64") + ".new")


def test_permissive_helpers_are_gone():
    for name in ("find_release", "select_asset", "parse_sha256sums"):
        assert not hasattr(su, name), name


# ---- refusals before any file exists ----------------------------------------------------------

def refused(monkeypatch, frozen_onefile, releases, match, key="linux-x64", tag=TAG):
    host(monkeypatch, key)
    no_download(monkeypatch)
    if getattr(su._open, "__name__", "") == "_open":
        no_network(monkeypatch)
    with pytest.raises(su.SelfUpdateError, match=match) as info:
        run(releases, key, tag)
    nothing_staged(frozen_onefile.parent)
    return info.value


@pytest.mark.parametrize("names,match", [
    ([canonical("linux-x64") + ".zip"], "no exact asset"),
    ([canonical("linux-x64") + ".tar.gz"], "no exact asset"),
    (["cyber-controller-linux-x64"], "no exact asset"),
    ([canonical("linux-x64") + "-debug"], "no exact asset"),
    ([canonical("linux-x64", "v9.9.8")], "no exact asset"),
    ([canonical("linux-x64"), canonical("linux-x64")], "ambiguous asset"),
], ids=["zip", "tar-gz", "no-tag", "debug-suffix", "wrong-tag", "duplicate"])
def test_non_canonical_catalogs_are_refused_before_download(monkeypatch, frozen_onefile, names,
                                                            match):
    assets = [asset(n, x64()) for n in names] + [sums_asset()]
    info = refused(monkeypatch, frozen_onefile, [release(assets)], match)
    assert isinstance(info.__cause__, us.ReleaseSelectionError)


def test_installer_only_release_is_refused_for_the_windows_swap(monkeypatch, frozen_onefile):
    setup = asset(canonical("windows-x64").replace(".exe", "-setup.exe"), b"installer bytes")
    refused(monkeypatch, frozen_onefile, [release([setup, sums_asset()])], "no exact asset",
            key="windows-x64")


def entry(**flags):
    return release([asset(canonical("linux-x64"), x64()), sums_asset()], **flags)


@pytest.mark.parametrize("releases,tag,match", [
    ([entry()], "9.9.9", "no release with exact tag"),
    ([entry(draft=True)], TAG, "not explicitly published"),
    ([entry(prerelease=True)], TAG, "not explicitly stable"),
    ([{"tag_name": TAG, "assets": entry()["assets"]}], TAG, "not explicitly published"),
    ([entry(), entry(draft=True)], TAG, "ambiguous release tag"),
    ([], TAG, "no release with exact tag"),
], ids=["digit-equivalent-tag", "draft", "prerelease", "flagless", "same-tag-draft-beside-stable",
        "empty"])
def test_release_identity_is_pinned_before_download(monkeypatch, frozen_onefile, releases, tag,
                                                    match):
    info = refused(monkeypatch, frozen_onefile, releases, match, tag=tag)
    assert isinstance(info.__cause__, us.ReleaseSelectionError)


@pytest.mark.parametrize("size,match", [
    ("missing", "size must be an integer"),
    (True, "size must be an integer"),
    ("123", "size must be an integer"),
    (0, "size must be positive"),
    (us.MAX_ASSET_BYTES + 1, "exceeds the maximum"),
], ids=["missing", "bool", "string", "zero", "over-cap"])
def test_published_size_is_validated_before_download(monkeypatch, frozen_onefile, size, match):
    binary = asset(canonical("linux-x64"), x64())
    if size == "missing":
        del binary["size"]
    else:
        binary["size"] = size
    refused(monkeypatch, frozen_onefile, [release([binary, sums_asset()])], match)


def manifest_case(monkeypatch, frozen_onefile, match, assets=None, manifest=None):
    data = x64()
    releases, payloads = catalog("linux-x64", data, manifest=manifest)
    if assets is not None:
        releases[0]["assets"] = assets
    _, responses = transport(monkeypatch, payloads)
    refused(monkeypatch, frozen_onefile, releases, match)
    return responses


def test_missing_manifest_is_refused_before_download(monkeypatch, frozen_onefile):
    manifest_case(monkeypatch, frozen_onefile, "SHA256SUMS", assets=[asset(canonical("linux-x64"),
                                                                           x64())])


def test_two_manifests_are_refused_as_ambiguous(monkeypatch, frozen_onefile):
    manifest_case(monkeypatch, frozen_onefile, "ambiguous manifest",
                  assets=[asset(canonical("linux-x64"), x64()), sums_asset(), sums_asset()])


def test_manifest_beyond_the_bound_is_refused_and_read_only_to_the_bound(monkeypatch,
                                                                         frozen_onefile):
    good = sums_text([(canonical("linux-x64"), x64())]).encode()
    padding = b"# " + b"x" * (su.MAX_SUMS_BYTES - len(good)) + b"\n"
    responses = manifest_case(monkeypatch, frozen_onefile, "exceeds", manifest=good + padding)
    assert responses[0].requested == [su.MAX_SUMS_BYTES + 1], "one bounded read, nothing more"


def test_conflicting_digests_for_the_asset_are_refused(monkeypatch, frozen_onefile):
    name = canonical("linux-x64")
    manifest = sums_text([(name, x64()), (name, b"other")]).encode()
    manifest_case(monkeypatch, frozen_onefile, "ambiguous checksum", manifest=manifest)


def test_manifest_without_the_exact_name_is_refused(monkeypatch, frozen_onefile):
    manifest = sums_text([(canonical("linux-x64") + ".zip", x64())]).encode()
    manifest_case(monkeypatch, frozen_onefile, "no checksum published", manifest=manifest)


def test_undecodable_manifest_is_refused(monkeypatch, frozen_onefile):
    manifest_case(monkeypatch, frozen_onefile, "not UTF-8", manifest=bytes([0xFF, 0xFE]) * 8)


# ---- the download boundary: ownership and size ------------------------------------------------

def test_overrun_is_refused_before_any_excess_byte_is_written(monkeypatch, frozen_onefile):
    host(monkeypatch, "linux-x64")
    data = x64()
    releases, payloads = catalog("linux-x64", data)
    payloads[BASE + canonical("linux-x64")] = data + b"unexpected trailing bytes"
    _, responses = transport(monkeypatch, payloads)
    with pytest.raises(su.SelfUpdateError, match="exceeds the published size"):
        run(releases)
    nothing_staged(frozen_onefile.parent)
    assert responses[-1].served == len(data) + 1, "read exactly one byte past the size, no more"


def test_short_stream_is_refused_and_the_part_removed(monkeypatch, frozen_onefile):
    host(monkeypatch, "linux-x64")
    data = x64()
    releases, payloads = catalog("linux-x64", data)
    payloads[BASE + canonical("linux-x64")] = data[:-1]
    transport(monkeypatch, payloads)
    with pytest.raises(su.SelfUpdateError, match="ended at"):
        run(releases)
    nothing_staged(frozen_onefile.parent)


def test_pre_existing_part_is_never_touched_and_the_asset_never_fetched(monkeypatch,
                                                                         frozen_onefile):
    host(monkeypatch, "linux-x64")
    data = x64()
    releases, payloads = catalog("linux-x64", data)
    part = frozen_onefile.parent / (canonical("linux-x64") + ".part")
    part.write_bytes(b"someone else's file")
    opened, _ = transport(monkeypatch, payloads)
    with pytest.raises(su.SelfUpdateError, match="refusing to overwrite"):
        run(releases)
    assert part.read_bytes() == b"someone else's file"
    assert opened == [SUMS_URL], "ownership is claimed before the asset transport is opened"
    assert not list(frozen_onefile.parent.glob("*.new"))


def test_failed_transport_removes_only_this_attempts_file(monkeypatch, frozen_onefile):
    host(monkeypatch, "linux-x64")
    data = x64()
    releases, payloads = catalog("linux-x64", data)
    del payloads[BASE + canonical("linux-x64")]
    foreign = frozen_onefile.parent / "other-app.part"
    foreign.write_bytes(b"not ours")
    transport(monkeypatch, payloads)
    with pytest.raises(su.SelfUpdateError, match="download failed"):
        run(releases)
    assert not (frozen_onefile.parent / (canonical("linux-x64") + ".part")).exists()
    assert foreign.read_bytes() == b"not ours"
    assert frozen_onefile.read_bytes() == b"current build (double)"


@pytest.mark.parametrize("size", [0, -1, True, 3.0, None], ids=["zero", "negative", "bool", "float",
                                                              "none"])
def test_download_asset_requires_a_positive_published_size(monkeypatch, tmp_path, size):
    no_network(monkeypatch)
    dest = tmp_path / "x.part"
    with pytest.raises(su.SelfUpdateError, match="positive integer"):
        su.download_asset("https://example.invalid/x", str(dest), expected_size=size)
    assert not dest.exists()


def test_failed_file_object_construction_closes_the_descriptor_then_removes_only_its_file(
        monkeypatch, tmp_path):
    no_network(monkeypatch)
    dest = tmp_path / "asset.part"
    foreign = tmp_path / "other.part"
    foreign.write_bytes(b"not ours")
    fds, closed = [], []
    real_open, real_close = os.open, os.close

    def record_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        fds.append(fd)
        return fd

    def record_close(fd):
        closed.append(fd)
        real_close(fd)

    def broken_fdopen(fd, *args, **kwargs):
        raise OSError(24, "injected: file-object construction failed")

    monkeypatch.setattr(su.os, "open", record_open)
    monkeypatch.setattr(su.os, "close", record_close)
    monkeypatch.setattr(su.os, "fdopen", broken_fdopen)
    with pytest.raises(su.SelfUpdateError, match="could not open") as info:
        su.download_asset("https://example.invalid/asset", str(dest), expected_size=10)
    assert isinstance(info.value.__cause__, OSError), "original context preserved"
    assert closed == fds and len(fds) == 1, "the raw descriptor was closed before removal"
    assert not dest.exists(), "the owned partial was removed (needs the close first on Windows)"
    assert foreign.read_bytes() == b"not ours"


def test_successful_transfer_hands_the_descriptor_to_the_file_object_and_releases_it(
        monkeypatch, tmp_path):
    data = b"exactly ten"
    transport(monkeypatch, {"https://example.invalid/asset": data})
    handles = []
    real_fdopen = os.fdopen

    def record_fdopen(fd, *args, **kwargs):
        fh = real_fdopen(fd, *args, **kwargs)
        handles.append(fh)
        return fh

    monkeypatch.setattr(su.os, "fdopen", record_fdopen)
    dest = tmp_path / "asset.part"
    got = su.download_asset("https://example.invalid/asset", str(dest), expected_size=len(data))
    assert got == str(dest) and dest.read_bytes() == data
    assert len(handles) == 1 and handles[0].closed, "released by the ordinary context manager"
