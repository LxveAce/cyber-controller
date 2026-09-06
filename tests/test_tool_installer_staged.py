"""Staged, cancellable download-install transaction (src/core/tool_installer.py).

The network is mocked (urlopen serves a synthetic in-memory zip) and the launch probe is stubbed, so no real
download or binary execution happens. Verifies the transaction stages, publishes atomically, honours cancel /
begin_commit, and never touches a prior install on failure/cancel — mirroring the bundled transaction.
"""
from __future__ import annotations

import hashlib
import io
import os
import zipfile

import pytest

from src.core import tool_installer as ti
from src.core.tool_bundle import ToolCancelled


class _CM:
    def __init__(self, raw):
        self._raw = raw

    def __enter__(self):
        return self._raw

    def __exit__(self, *_a):
        self._raw.close()
        return False


def _zip_bytes(body=b"MZ stub"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pfx/aircrack-ng.exe", body)
    return buf.getvalue()


def _spec(zip_bytes, *, sha256=None):
    return ti.ToolInstallSpec(
        tool="aircrack-ng", os_key="windows", version="1.7", url="http://example/ac.zip", archive="zip",
        member_prefix="pfx/", exe_name="aircrack-ng.exe", license="GPL",
        sha256=(sha256 if sha256 is not None else hashlib.sha256(zip_bytes).hexdigest()),
        size_bytes=len(zip_bytes))


def _wire(monkeypatch, zip_bytes):
    monkeypatch.setattr(ti.urllib.request, "urlopen",
                        lambda req, timeout=None: _CM(io.BytesIO(zip_bytes)))
    monkeypatch.setattr(ti, "_launches", lambda _p: True)   # stub the launch probe (no real exec)


def _seed_prior(tools_dir):
    prior = tools_dir / "aircrack-ng"
    prior.mkdir(parents=True)
    (prior / "aircrack-ng.exe").write_bytes(b"OLD-INSTALL")
    return prior


def test_install_publishes_atomically_and_cleans_staging(tmp_path, monkeypatch):
    z = _zip_bytes()
    _wire(monkeypatch, z)
    tools = tmp_path / "tools"
    exe = ti.install_tool(_spec(z), directory=str(tools))
    assert exe.endswith("aircrack-ng.exe") and os.path.isfile(exe)
    assert not any(p.name.startswith((".cc-dl-stage-", ".cc-dl-backup-")) for p in tools.iterdir())


def test_install_replaces_prior_and_leaves_no_backup(tmp_path, monkeypatch):
    z = _zip_bytes(b"MZ NEW")
    _wire(monkeypatch, z)
    tools = tmp_path / "tools"
    prior = _seed_prior(tools)
    ti.install_tool(_spec(z), directory=str(tools))
    assert (prior / "aircrack-ng.exe").read_bytes() == b"MZ NEW"      # published
    assert not any(p.name.startswith((".cc-dl-stage-", ".cc-dl-backup-")) for p in tools.iterdir())


def test_cancel_before_commit_leaves_prior_intact(tmp_path, monkeypatch):
    z = _zip_bytes()
    _wire(monkeypatch, z)
    tools = tmp_path / "tools"
    prior = _seed_prior(tools)
    with pytest.raises(ToolCancelled):
        ti.install_tool(_spec(z), directory=str(tools), should_cancel=lambda: True)
    assert (prior / "aircrack-ng.exe").read_bytes() == b"OLD-INSTALL"   # prior untouched
    assert not any(p.name.startswith(".cc-dl-stage-") for p in tools.iterdir())


def test_bad_integrity_leaves_prior_intact(tmp_path, monkeypatch):
    z = _zip_bytes()
    _wire(monkeypatch, z)
    tools = tmp_path / "tools"
    prior = _seed_prior(tools)
    with pytest.raises(RuntimeError):
        ti.install_tool(_spec(z, sha256="f" * 64), directory=str(tools))   # wrong hash
    assert (prior / "aircrack-ng.exe").read_bytes() == b"OLD-INSTALL"


def test_begin_commit_fires_once_at_the_boundary(tmp_path, monkeypatch):
    z = _zip_bytes()
    _wire(monkeypatch, z)
    tools = tmp_path / "tools"
    prior = _seed_prior(tools)
    calls = []

    def bc():
        calls.append(1)
        assert (prior / "aircrack-ng.exe").read_bytes() == b"OLD-INSTALL"   # prior still in place at boundary

    ti.install_tool(_spec(z), directory=str(tools), begin_commit=bc)
    assert calls == [1]


def test_begin_commit_raise_aborts_before_publish(tmp_path, monkeypatch):
    z = _zip_bytes()
    _wire(monkeypatch, z)
    tools = tmp_path / "tools"
    prior = _seed_prior(tools)

    def bc():
        raise ToolCancelled("cancelled at the commit boundary")

    with pytest.raises(ToolCancelled):
        ti.install_tool(_spec(z), directory=str(tools), begin_commit=bc)
    assert (prior / "aircrack-ng.exe").read_bytes() == b"OLD-INSTALL"
    assert not any(p.name.startswith((".cc-dl-stage-", ".cc-dl-backup-")) for p in tools.iterdir())


def test_download_reports_byte_progress(tmp_path, monkeypatch):
    z = _zip_bytes(b"x" * 5000)
    _wire(monkeypatch, z)
    tools = tmp_path / "tools"
    seen = []
    ti.install_tool(_spec(z), directory=str(tools), on_progress=lambda w, t: seen.append((w, t)))
    assert seen, "progress should be reported at least once"
    assert seen[-1] == (len(z), len(z))     # final report: all bytes downloaded, known total
    assert all(0 <= w <= len(z) and t == len(z) for w, t in seen)


# ── D1: cancellation is cooperative — no network/reads/probe after a pending cancel ──

class _CountingResp:
    def __init__(self, data):
        self._buf = io.BytesIO(data)
        self.reads = 0

    def read(self, n=-1):
        self.reads += 1
        return self._buf.read(n)

    def close(self):
        self._buf.close()


def test_pending_cancel_never_starts_network(tmp_path, monkeypatch):
    z = _zip_bytes()
    opens = []
    monkeypatch.setattr(ti.urllib.request, "urlopen",
                        lambda req, timeout=None: opens.append(1) or _CM(io.BytesIO(z)))
    monkeypatch.setattr(ti, "_launches", lambda _p: True)
    with pytest.raises(ToolCancelled):
        ti.install_tool(_spec(z), directory=str(tmp_path / "tools"), should_cancel=lambda: True)
    assert opens == []                       # a pending cancel opened no network


def test_cancel_after_first_chunk_stops_further_reads(tmp_path, monkeypatch):
    z = _zip_bytes(b"x" * 200000)            # several 64 KiB chunks
    resp = _CountingResp(z)
    monkeypatch.setattr(ti.urllib.request, "urlopen", lambda req, timeout=None: _CM(resp))
    monkeypatch.setattr(ti, "_launches", lambda _p: True)
    with pytest.raises(ToolCancelled):
        ti.install_tool(_spec(z), directory=str(tmp_path / "tools"),
                        should_cancel=lambda: resp.reads >= 1)   # pending after the first read
    assert resp.reads == 1                   # stopped before reading the rest of the body


def test_cancel_after_download_does_not_enter_probe(tmp_path, monkeypatch):
    z = _zip_bytes()
    monkeypatch.setattr(ti.urllib.request, "urlopen", lambda req, timeout=None: _CM(io.BytesIO(z)))
    launched = []
    monkeypatch.setattr(ti, "_launches", lambda _p: launched.append(1) or True)
    done = {"v": False}
    with pytest.raises(ToolCancelled):
        ti.install_tool(_spec(z), directory=str(tmp_path / "tools"),
                        on_progress=lambda w, t: done.__setitem__("v", bool(t) and w >= t),
                        should_cancel=lambda: done["v"])   # pending once the download finishes
    assert launched == []                    # the launch probe is never entered after cancellation


# ── zip-bomb: expanded output is bounded even for a hash-pinned archive ──

def test_expanded_output_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(ti, "_MAX_EXPANDED_BYTES", 1000)
    z = _zip_bytes(b"x" * 5000)               # expands to 5000 > the (patched) 1000-byte ceiling
    _wire(monkeypatch, z)
    with pytest.raises(RuntimeError):
        ti.install_tool(_spec(z), directory=str(tmp_path / "tools"))


# ── destination borrowing: an async job that already owns the dest passes its lease ──

def test_install_borrows_a_lease_without_releasing_owner(tmp_path, monkeypatch):
    from src.core import tool_bundle
    z = _zip_bytes()
    _wire(monkeypatch, z)
    tools = tmp_path / "tools"
    tool_dir = os.path.join(str(tools), "aircrack-ng")
    lease = tool_bundle.acquire_destination(tool_dir)      # the async job owns the destination
    exe = ti.install_tool(_spec(z), directory=str(tools), lease=lease)   # borrows it
    assert os.path.isfile(exe)
    assert tool_bundle.release_destination(lease) is True   # borrow didn't release the owner's lease
    assert tool_bundle.release_destination(lease) is False


def test_install_rejects_a_lease_for_a_different_destination(tmp_path, monkeypatch):
    from src.core import tool_bundle
    z = _zip_bytes()
    _wire(monkeypatch, z)
    other = tool_bundle.acquire_destination(str(tmp_path / "tools" / "elsewhere"))
    with pytest.raises(tool_bundle.DestinationBusy):
        ti.install_tool(_spec(z), directory=str(tmp_path / "tools"), lease=other)   # lease not for this dest
    tool_bundle.release_destination(other)


# ── D2: a failed launch probe surfaces the REAL failure, not cancellation ──

def test_probe_failure_is_not_relabelled_cancelled(tmp_path, monkeypatch):
    z = _zip_bytes()
    monkeypatch.setattr(ti.urllib.request, "urlopen", lambda req, timeout=None: _CM(io.BytesIO(z)))
    flag = {"v": False}

    def failing_probe(_p):
        flag["v"] = True     # a cancel request arrives concurrently with the probe...
        return False         # ...but the probe genuinely FAILED (the flag didn't cause it)

    monkeypatch.setattr(ti, "_launches", failing_probe)
    prior = _seed_prior(tmp_path / "tools")
    with pytest.raises(RuntimeError) as ei:
        ti.install_tool(_spec(z), directory=str(tmp_path / "tools"), should_cancel=lambda: flag["v"])
    assert not isinstance(ei.value, ToolCancelled)                 # NOT relabelled as cancellation
    assert "would not launch" in str(ei.value)                    # the real acquisition failure is surfaced
    assert (prior / "aircrack-ng.exe").read_bytes() == b"OLD-INSTALL"


def test_successful_probe_with_late_cancel_is_cancelled(tmp_path, monkeypatch):
    z = _zip_bytes()
    monkeypatch.setattr(ti.urllib.request, "urlopen", lambda req, timeout=None: _CM(io.BytesIO(z)))
    flag = {"v": False}

    def ok_probe(_p):
        flag["v"] = True     # a cancel arrives just as the probe SUCCEEDS -> gate before commit
        return True

    monkeypatch.setattr(ti, "_launches", ok_probe)
    prior = _seed_prior(tmp_path / "tools")
    with pytest.raises(ToolCancelled):
        ti.install_tool(_spec(z), directory=str(tmp_path / "tools"), should_cancel=lambda: flag["v"])
    assert (prior / "aircrack-ng.exe").read_bytes() == b"OLD-INSTALL"   # a successful probe was not committed
