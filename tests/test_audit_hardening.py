"""Tests for the security-audit medium/low fixes: serial callback removal (M-1 enabler) and
admin_ip validation (M-4)."""
from __future__ import annotations

from src.core.serial_handler import SerialConnection
from src.core.backends import adb_backend


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
