"""PWA layer (MB cluster) — installable LAN wireless remote.

Asserts the manifest + service-worker routes serve correctly and PUBLICLY, the base template wires the PWA
head + SW registration, and — the security gate for this cluster — the service worker is structurally
incapable of caching authenticated `/api/` data or the `/socket.io/` serial stream.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytest.importorskip("flask")

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.security.web_auth import new_csrf_token
from src.ui.web.app import create_app

_SW = Path(__file__).resolve().parents[1] / "src" / "ui" / "web" / "static" / "sw.js"
_MANIFEST = _SW.parent / "manifest.webmanifest"


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")


def _client():
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool())
    return app.test_client()


def _authed_client():
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool())
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["cred_gen"] = client.application.extensions["cc_web_credentials"].generation
        sess["csrf"] = new_csrf_token()
    return client


# ── routes serve, publicly ───────────────────────────────────────────

def test_manifest_route_is_public_valid_json():
    resp = _client().get("/manifest.webmanifest")   # NO auth session
    assert resp.status_code == 200
    assert "application/manifest+json" in resp.headers["Content-Type"]
    data = json.loads(resp.get_data(as_text=True))
    assert data["start_url"] == "/" and data["scope"] == "/"
    assert data["display"] == "standalone"
    assert data["theme_color"] == "#a371f7"
    assert len(data["icons"]) >= 2
    assert all("maskable" in ic["purpose"] for ic in data["icons"])


def test_sw_route_public_root_scope_headers():
    resp = _client().get("/sw.js")                   # NO auth session
    assert resp.status_code == 200
    assert "javascript" in resp.headers["Content-Type"]
    assert resp.headers.get("Service-Worker-Allowed") == "/"   # root scope despite the /sw.js path
    assert "no-cache" in resp.headers.get("Cache-Control", "")


def test_base_template_wires_pwa_head_and_registration():
    body = _authed_client().get("/").get_data(as_text=True)
    assert 'rel="manifest"' in body and 'href="/manifest.webmanifest"' in body
    assert 'name="theme-color"' in body and "#a371f7" in body
    assert 'rel="apple-touch-icon"' in body
    assert "serviceWorker" in body and "/sw.js" in body


# ── the security invariant: the SW cannot cache authenticated data ───

def _sw_text():
    return _SW.read_text(encoding="utf-8")


def _shell_assets():
    text = _sw_text()
    start = text.index("const SHELL_ASSETS = [")
    block = text[start:text.index("];", start)]
    return [ln.strip().strip("',") for ln in block.splitlines() if ln.strip().startswith("'")]


def test_shell_allowlist_is_static_only():
    assets = _shell_assets()
    assert assets, "SHELL_ASSETS should not be empty"
    for a in assets:
        assert not a.startswith("/api"), f"authenticated API path in shell allowlist: {a}"
        # The LIVE event stream lives at the root '/socket.io/...' and must never be cached; the vendored
        # CLIENT library at '/static/vendor/socket.io.min.js' is a static shell asset and is fine.
        assert not a.startswith("/socket.io"), f"live serial stream in shell allowlist: {a}"
        assert "://" not in a, f"cross-origin URL in shell allowlist: {a}"
        assert a.startswith("/static/") or a == "/manifest.webmanifest", f"non-shell path: {a}"


def test_sw_fetch_guards_reject_sensitive_paths():
    text = _sw_text()
    # isShellAsset — the single gate to the cache — must reject every sensitive/uncacheable class.
    assert "request.method !== 'GET'" in text          # non-GET never cached
    assert "self.location.origin" in text              # cross-origin never cached
    assert "startsWith('/api/')" in text               # authenticated API data never cached
    assert "startsWith('/socket.io/')" in text         # live serial/event stream never cached


def test_sw_cache_write_is_downstream_of_the_guard():
    text = _sw_text()
    # Exactly one cache write and one respondWith, and both live behind the isShellAsset early-return.
    assert text.count("cache.put(") == 1
    assert text.count("respondWith(") == 1
    guard = text.index("if (!isShellAsset(request))")
    assert guard < text.index("respondWith("), "respondWith must be gated by the isShellAsset early-return"
    assert guard < text.index("cache.put("), "cache write must be gated by the isShellAsset early-return"


def test_manifest_file_matches_route_and_is_valid():
    data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    assert data["name"] == "Cyber Controller"
    assert data["background_color"] and data["theme_color"]
    for ic in data["icons"]:
        assert ic["src"].startswith("/static/icons/")   # owner-drop location


def _node_bin():
    return shutil.which("node") or next(
        (p for p in (r"C:\nvm4w\nodejs\node.exe", "/c/nvm4w/nodejs/node.exe") if os.path.exists(p)), None)


_DIAG_MAX_STDERR_LINES = 15
_DIAG_MAX_LINE_CHARS = 200       # per-line cap: one giant line cannot inflate the report
_DIAG_MAX_STDERR_CHARS = 2000    # cap decoded stderr BEFORE splitting/formatting
_DIAG_MAX_CHARS = 500            # stdout-tail cap
_DIAG_MAX_TOTAL_CHARS = 4000     # final total-output cap (covers metadata + fallback)


def _diag_decode(raw):
    """Decode child output to text, never raising. Bytes are decoded with replacement (so real newlines are
    preserved rather than rendered as escapes by str(bytes)); anything else is str()."""
    try:
        if raw is None:
            return ""
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw).decode("utf-8", "replace")
        return str(raw)
    except Exception:  # noqa: BLE001 — decoding must never raise into the diagnostic
        return ""


def _sw_gate_diagnostic(kind, elapsed, stdout, stderr, **info):
    """Build a BOUNDED, non-raising failure diagnostic for the SW-gate harness subprocess. Names the failure
    kind (timeout / nonzero-exit / malformed-output), the within-process monotonic elapsed, and the last few
    STDERR phase markers — never an unbounded dump. The output is bounded three ways: each line is capped, the
    decoded stderr is capped before splitting, and the whole report (metadata and fallback included) is capped.
    Read the markers carefully: a MISSING marker does not prove the child never started, and a FINAL marker
    does not prove stdout was flushed or the process exited. This function never raises, so a problem building
    the diagnostic cannot mask the original test outcome."""
    try:
        parts = ["SW-gate harness failed: %s" % kind]
        if elapsed is not None:
            parts.append("elapsed=%.1fs (within-process monotonic; not a cross-process clock)" % elapsed)
        for key, value in info.items():
            parts.append(("%s=%r" % (key, value))[:_DIAG_MAX_LINE_CHARS])  # bound each metadata line
        text = _diag_decode(stderr)[-_DIAG_MAX_STDERR_CHARS:]              # decode + keep the recent tail, bounded
        lines = [ln[:_DIAG_MAX_LINE_CHARS] for ln in text.splitlines() if ln.strip()][-_DIAG_MAX_STDERR_LINES:]
        tail = "\n  ".join(lines)
        parts.append("last stderr phase markers (bounded; absence != 'never started', final != 'flushed/exited'):"
                     + ("\n  " + tail if tail else " <none captured>"))
        preview = _diag_decode(stdout)[-_DIAG_MAX_CHARS:]
        parts.append("stdout tail (bounded, %d chars): %r" % (len(preview), preview))
        report = "\n".join(parts)
        if len(report) > _DIAG_MAX_TOTAL_CHARS:
            report = report[:_DIAG_MAX_TOTAL_CHARS - 20] + " ...[diag truncated]"
        return report
    except Exception as diag_exc:  # noqa: BLE001 — diagnostics must never mask the underlying failure
        return ("SW-gate harness failed: %s (diagnostic unavailable: %r)" % (kind, diag_exc))[:_DIAG_MAX_TOTAL_CHARS]


def _run_sw_gate_harness(node, harness, timeout=30):
    """Run the Node SW-gate harness and return its STDOUT on success (the JSON contract, unchanged). On a
    failed run raise AssertionError carrying a bounded diagnostic that PRESERVES the original cause and
    distinguishes timeout vs nonzero-exit. Uses within-process monotonic elapsed only (never subtracts two
    processes' absolute clocks). Does not weaken any assertion or the 30-second deadline."""
    start = time.monotonic()
    try:
        proc = subprocess.run([node, harness], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(_sw_gate_diagnostic(
            "timeout", time.monotonic() - start, exc.stdout, exc.stderr, timeout_s=timeout)) from exc
    elapsed = time.monotonic() - start
    if proc.returncode != 0:
        # Preserve the underlying nonzero-exit as an explicit cause (parity with the timeout/JSON paths, which
        # already chain their originals): the old check_output raised CalledProcessError, so keep one as __cause__.
        cause = subprocess.CalledProcessError(proc.returncode, [node, harness], output=proc.stdout,
                                              stderr=proc.stderr)
        raise AssertionError(_sw_gate_diagnostic(
            "nonzero-exit", elapsed, proc.stdout, proc.stderr, returncode=proc.returncode)) from cause
    return proc.stdout


def test_sw_gate_behavioral():
    """Execute the REAL isShellAsset (real URL parser) against adversarial URLs — the only test that would
    catch a gate regression (inverted return, reordered branch) the lexical checks would miss."""
    node = _node_bin()
    if not node:
        pytest.skip("node not available for the behavioral SW-gate harness")
    harness = Path(__file__).parent / "_sw_gate_harness.js"
    out = _run_sw_gate_harness(node, str(harness))
    try:
        results = json.loads(out)
    except json.JSONDecodeError as exc:
        raise AssertionError(_sw_gate_diagnostic("malformed-output", None, out, "", parse_error=str(exc))) from exc
    for r in results:
        assert r["got"] == r["want"], f"gate({r['name']}) returned {r['got']}, expected {r['want']}"
    names = {r["name"] for r in results}
    # confirm the adversarial classes were actually exercised (not a silently-empty pass)
    assert {"api", "socketio", "dashboard", "traversal", "enc_traversal",
            "crossorigin", "post_shell", "css", "manifest"} <= names
