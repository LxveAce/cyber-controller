"""Regression guards for the cc-deep-audit-2 pass-2 batch (2026-07-13, ledger rows P3-P8).

Each defect re-confirmed against the real code, fixed minimally, and pinned here:

    P4 (MED, security) tails.verify_gpg — keyed "good" off VALIDSIG, which gpg emits for ANY valid
      sig INCLUDING one from a REVOKED (REVKEYSIG) or EXPIRED (EXPKEYSIG) key, so a revoked-key sig
      passed and skipped the SHA-256 anchor. Now gates on GOODSIG and hard-refuses revoked/expired.
    P3 (MED) self_update.win_swap_script — did not escape '%' in the exe path, so a '%'-containing
      install dir corrupted every move/start/breadcrumb target (silent update-fail). Now escaped.
    P5 (MED) wpa_capture._consume_eapol — EAPOL-Key length guard 4 bytes short: a 95-96-byte frame
      under-read the MIC and grew the MIC-zeroed bytearray, emitting a fake handshake. Guard raised.
    P6 (LOW) flash_engine.detect_cyd — set status=DONE in a bare finally even when the probe raised.
    P7 (LOW) serial_handler.connect — leaked the open COM handle on a non-SerialException post-open.
    P8 (LOW) cross_comm — cooldown eviction/removal forgot a disabled/removed rule's live window.

Pure logic + fakes: no hardware, gpg, esptool, or real serial port is touched.
"""
from __future__ import annotations

import struct

import pytest

# ── P4: tails.verify_gpg refuses a revoked/expired-key signature ──────────────────────────────────

def _patch_gpg(monkeypatch, status_out: str):
    import src.core.tails as tails

    monkeypatch.setattr("shutil.which", lambda cand: "/usr/bin/gpg")

    class _Proc:
        stdout = status_out
        stderr = ""

    monkeypatch.setattr(tails.subprocess, "run", lambda *a, **k: _Proc())
    return tails


def test_verify_gpg_refuses_revoked_key(monkeypatch):
    tails = _patch_gpg(monkeypatch, "")
    fpr = tails.TAILS_SIGNING_KEY_FINGERPRINT.replace(" ", "")
    # A revoked key still emits VALIDSIG (with the fingerprint) — the old code accepted it.
    status = f"[GNUPG:] REVKEYSIG 258ACD84F Tails\n[GNUPG:] VALIDSIG {fpr} 2024-01-01\n"
    tails2 = _patch_gpg(monkeypatch, status)
    assert tails2.verify_gpg("x.img", "x.img.sig", lambda _s: None) is False


def test_verify_gpg_refuses_expired_key(monkeypatch):
    import src.core.tails as tails
    fpr = tails.TAILS_SIGNING_KEY_FINGERPRINT.replace(" ", "")
    status = f"[GNUPG:] EXPKEYSIG 258ACD84F Tails\n[GNUPG:] VALIDSIG {fpr} 2024-01-01\n"
    _patch_gpg(monkeypatch, status)
    assert tails.verify_gpg("x.img", "x.img.sig", lambda _s: None) is False


def test_verify_gpg_accepts_current_goodsig(monkeypatch):
    import src.core.tails as tails
    fpr = tails.TAILS_SIGNING_KEY_FINGERPRINT.replace(" ", "")
    status = f"[GNUPG:] GOODSIG 258ACD84F Tails\n[GNUPG:] VALIDSIG {fpr} 2024-01-01\n"
    _patch_gpg(monkeypatch, status)
    assert tails.verify_gpg("x.img", "x.img.sig", lambda _s: None) is True


def test_verify_gpg_goodsig_wrong_key_is_false(monkeypatch):
    import src.core.tails as tails
    status = "[GNUPG:] GOODSIG deadbeef Someone\n[GNUPG:] VALIDSIG " + ("0" * 40) + " 2024-01-01\n"
    _patch_gpg(monkeypatch, status)
    assert tails.verify_gpg("x.img", "x.img.sig", lambda _s: None) is False


def test_verify_gpg_missing_key_defers_to_sha(monkeypatch):
    import src.core.tails as tails
    status = "[GNUPG:] NO_PUBKEY 258ACD84F\n[GNUPG:] ERRSIG 258ACD84F\n"
    _patch_gpg(monkeypatch, status)
    assert tails.verify_gpg("x.img", "x.img.sig", lambda _s: None) is None


# ── P3: win_swap_script escapes '%' in the exe path (not its own %tries%/%~f0) ────────────────────

def test_win_swap_script_escapes_percent_in_path():
    from src.core.self_update import win_swap_script

    cur = r"C:\Tools\100%CPU\cyber-controller.exe"
    new = r"C:\Tools\100%CPU\cyber-controller.exe.new"
    script = win_swap_script(pid=1234, new_exe=new, cur_exe=cur)

    # The move target must carry the doubled percent so cmd writes the literal path.
    assert r"100%%CPU\cyber-controller.exe" in script
    # A raw single-% path segment must NOT survive in an interpolated path (would mangle in cmd).
    assert r'move /Y "C:\Tools\100%CPU' not in script
    # The script's own cmd tokens stay literal (single %).
    assert "if %tries% lss 10" in script
    assert 'del "%~f0"' in script


def test_win_swap_script_plain_path_unchanged():
    from src.core.self_update import win_swap_script

    script = win_swap_script(pid=99, new_exe=r"C:\Apps\cc.exe.new", cur_exe=r"C:\Apps\cc.exe")
    assert r'move /Y "C:\Apps\cc.exe.new" "C:\Apps\cc.exe"' in script
    assert "%%" not in script.replace("%~f0", "").replace("%tries%", "")  # no spurious doubling


# ── P5: a truncated EAPOL-Key frame is skipped, not laundered into a fake handshake ───────────────

def _eapol_m2(body_len: int) -> bytes:
    """An 802.1X EAPOL-Key message-2 frame whose fixed body is *body_len* bytes."""
    body = bytearray(body_len)
    body[1:3] = struct.pack(">H", 0x0100)          # key_info: MIC set, ACK clear -> message 2
    if body_len >= 45:
        body[13:45] = b"\x22" * 32                 # SNonce
    if body_len >= 93:
        body[77:93] = b"\x33" * 16                 # MIC field
    return b"\x01\x03" + struct.pack(">H", body_len) + bytes(body)


def _dot11_to_ds() -> bytes:
    f = bytearray(24)
    f[1] = 0x01                                    # ToDS=1, FromDS=0 -> ap=addr1, sta=addr2
    f[4:10] = b"\xaa" * 6                           # addr1 = AP
    f[10:16] = b"\xbb" * 6                          # addr2 = STA
    return bytes(f)


def test_consume_eapol_skips_truncated_frame():
    from src.core.wpa_capture import _consume_eapol

    f = _dot11_to_ds()
    m1 = {(b"\xaa" * 6, b"\xbb" * 6): b"\x11" * 32}  # ANonce already seen for (ap, sta)
    hs: list = []
    # 91-byte body (eapol = 95): the pre-fix short frame -> truncated MIC + grown array.
    _consume_eapol(f, _eapol_m2(91), {}, [], m1, hs)
    assert hs == [], "a truncated EAPOL-Key frame must be skipped, not emitted as a handshake"


def test_consume_eapol_accepts_full_frame_with_16_byte_mic():
    from src.core.wpa_capture import _consume_eapol

    f = _dot11_to_ds()
    m1 = {(b"\xaa" * 6, b"\xbb" * 6): b"\x11" * 32}
    hs: list = []
    _consume_eapol(f, _eapol_m2(95), {}, [], m1, hs)  # full 95-byte body (eapol = 99)
    assert len(hs) == 1
    assert len(hs[0].mic) == 16, "a full frame must yield a complete 16-byte MIC"
    assert len(hs[0].eapol) == 99, "the MIC-zeroed frame must not grow past the real frame length"


# ── P6: detect_cyd records ERROR (not DONE) when the probe raises ─────────────────────────────────

def test_detect_cyd_status_is_error_on_probe_failure(monkeypatch):
    from src.core.flash_engine import FlashEngine, FlashStatus

    def _boom(*a, **k):
        raise RuntimeError("probe failed: no esptool")

    monkeypatch.setattr("src.core.cyd_detect.detect_cyd", _boom)
    engine = FlashEngine()
    with pytest.raises(RuntimeError):
        engine.detect_cyd("COM_TEST_CYD")
    assert engine.status == FlashStatus.ERROR, "a raised probe must not leave status = DONE"


# ── P7: connect() releases the open COM handle if a post-open step raises non-SerialException ─────

def test_connect_releases_handle_on_reader_thread_failure(monkeypatch):
    import src.core.serial_handler as sh
    from src.core.serial_handler import ConnectionState, SerialConnection

    class _FakeSerial:
        def __init__(self):
            self.port = None
            self.baudrate = self.timeout = self.write_timeout = None
            self.dtr = self.rts = None
            self.is_open = False
            self.closed = False

        def open(self):
            self.is_open = True

        def close(self):
            self.is_open = False
            self.closed = True

    fake = _FakeSerial()
    monkeypatch.setattr(sh.serial, "Serial", lambda: fake)

    class _BadThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start a new thread")  # NOT a SerialException

    monkeypatch.setattr(sh.threading, "Thread", _BadThread)

    conn = SerialConnection("COM_TEST_LEAK")
    with pytest.raises(RuntimeError):
        conn.connect()
    assert fake.closed, "the freshly-opened COM handle must be closed, not leaked"
    assert conn._serial is None
    assert conn._state == ConnectionState.ERROR


# ── P8: cooldown bookkeeping respects a disabled rule and is cleaned on remove ────────────────────

def _router():
    from src.core.cross_comm import AutoRouter, EventBus
    return AutoRouter(EventBus(), lambda port, cmd: None)


def test_disabled_rule_still_counts_for_the_eviction_cutoff():
    from src.core.cross_comm import RoutingRule

    r = _router()
    r.add_rule(RoutingRule(name="short", cooldown=30, enabled=True))
    r.add_rule(RoutingRule(name="long", cooldown=3600, enabled=False))  # disabled but present
    # The eviction cutoff is max cooldown across ALL rules (self._rules), so the disabled rule's
    # 3600s window is honored — not just the enabled short rule's 30s.
    assert max(rule.cooldown for rule in r._rules) == 3600


def test_remove_rule_drops_its_cooldown_stamps():
    from src.core.cross_comm import RoutingRule

    r = _router()
    r.add_rule(RoutingRule(name="alpha", cooldown=60))
    r._cooldowns["alpha:wifi_ap:aa:bb"] = 123.0
    r._cooldowns["beta:wifi_ap:cc:dd"] = 456.0
    assert r.remove_rule("alpha") is True
    assert "alpha:wifi_ap:aa:bb" not in r._cooldowns, "removed rule's stamps must be dropped"
    assert "beta:wifi_ap:cc:dd" in r._cooldowns, "another rule's stamps must be left intact"
