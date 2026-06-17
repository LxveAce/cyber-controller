"""Tests for the security-audit fixes: serial callback removal (M-1 enabler), admin_ip
validation (M-4), vault API SSRF allowlist (M-2), and Windows ACL hardening (L-1)."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from src.core.serial_handler import SerialConnection
from src.core.backends import adb_backend
from src.core import firmware_vault
from src.security import win_acl
from src.security.audit_trail import AuditTrail


# ── M-1: serial callback removal ─────────────────────────────────────

def test_remove_line_callback_is_idempotent():
    conn = SerialConnection("COM_TEST")  # not connected; we only touch the callback list
    seen = []
    def cb(line):
        seen.append(line)
    conn.on_line(cb)
    assert cb in conn._line_callbacks
    conn.remove_line_callback(cb)
    assert cb not in conn._line_callbacks
    conn.remove_line_callback(cb)  # removing again must not raise


# ── M-4: admin_ip validation ─────────────────────────────────────────

def test_install_rayhunter_rejects_non_ip_admin_ip():
    # A non-IP admin_ip is rejected up front (returns 1) before any adb/network work.
    lines = []
    rc = adb_backend.install_rayhunter(lines.append, admin_ip="evil.example.com")
    assert rc == 1
    assert any("invalid admin_ip" in l for l in lines)


def test_install_rayhunter_rejects_url_admin_ip():
    lines = []
    rc = adb_backend.install_rayhunter(lines.append, admin_ip="http://169.254.169.254/")
    assert rc == 1


# ── M-2: vault GitHub-API GETs go through the SSRF allowlist ──────────

def test_safe_api_get_json_rejects_off_allowlist_host():
    # Must raise BEFORE any network I/O — _require_allowed_url rejects the host.
    with pytest.raises(ValueError):
        firmware_vault._safe_api_get_json("https://evil.example.com/repos/x/y/releases/latest")


def test_safe_api_get_json_rejects_non_https():
    with pytest.raises(ValueError):
        firmware_vault._safe_api_get_json("http://api.github.com/repos/x/y/releases/latest")


# ── L-1: Windows NTFS ACL hardening ──────────────────────────────────

def test_restrict_to_current_user_is_noop_off_windows():
    if sys.platform == "win32":
        pytest.skip("Windows-specific no-op assertion")
    # Off Windows the helper must not touch anything and simply report False.
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "k"
        f.write_bytes(b"x" * 32)
        assert win_acl.restrict_to_current_user(f) is False


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS ACLs are Windows-only")
def test_restrict_to_current_user_strips_inherited_aces():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "secret.key"
        f.write_bytes(b"x" * 32)
        assert win_acl.restrict_to_current_user(f) is True
        # Still readable by the owner (us) after locking down.
        assert f.read_bytes() == b"x" * 32
        acl = subprocess.run(["icacls", str(f)], capture_output=True, text=True).stdout
        # The broad inherited principals must be gone; SYSTEM must remain.
        assert "BUILTIN\\Users" not in acl
        assert "Authenticated Users" not in acl
        assert "SYSTEM" in acl


# ── L-2: durable, tamper-evident audit trail ─────────────────────────

def test_audit_trail_persists_and_reloads_verified():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "audit-trail.jsonl"
        a = AuditTrail(persist_path=path)
        a.record("app_start", {})
        a.record("web_auth_ok", {"user": "lxve"})
        a.record("flash", {"port": "COM9", "profile": "ghostesp"})
        assert path.exists()
        # A fresh instance loads the prior chain and it verifies intact.
        b = AuditTrail(persist_path=path)
        assert b.length == 3
        ok, bad = b.verify_integrity()
        assert ok and bad == -1
        assert [e.action for e in b.entries] == ["app_start", "web_auth_ok", "flash"]


def test_audit_trail_detects_on_disk_tampering():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "audit-trail.jsonl"
        a = AuditTrail(persist_path=path)
        a.record("web_auth_fail", {"user": "mallory"})
        a.record("flash", {"port": "COM9"})
        # Tamper: flip a detail in the first persisted line without fixing the hash chain.
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[0] = lines[0].replace("mallory", "nobody")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # Reload: the chain must no longer verify (construction warns, never raises).
        b = AuditTrail(persist_path=path)
        ok, bad = b.verify_integrity()
        assert not ok
        assert bad == 0
