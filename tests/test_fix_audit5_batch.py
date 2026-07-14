"""Regression guards for the cc-deep-audit-5 pass-5 batch (2026-07-13, ledger pass5 rows S1-S4).

Each defect was re-confirmed against the real code (final-gate, verify-never-fake), fixed
minimally, and is pinned here:

    S1 (MED) wardrive.parse_nmea — a fix reported at exactly 0,0 ("Null Island") was accepted as a
      valid position (has_fix=True) and stamped onto WiGLE rows: _dm_to_dd rejected only out-of-
      range magnitudes, and has_fix only null-checked lat/lon, so 0.0 (in-range, not None) passed.
      Now both-zero collapses to no-position; a real equator/prime-meridian fix (only ONE coord at
      0) is preserved.
    S2 (MED) nodes_controller.detach — teardown ran UNLOCKED after popping node_id from _links, so a
      concurrent attach(node_id) could slip past the "already attached" guard and re-open the link /
      re-register the device while detach then closed the dongle + removed the just-attached device
      (phantom node on a closed port). detach now holds the RLock across the ENTIRE teardown, like
      attach().
    S3 (LOW) self_update.clear_failed_update — globbed the exe directory without glob.escape(), so a
      glob metacharacter in the install path ([ ] ? * — all legal folder chars) made the orphan-.new
      sweep match nothing (leftovers linger) or the wrong files. Now the directory is escaped.
    S4 (LOW) wordlist_manager.download_wordlist — the final atomic os.replace was the only failure
      path NOT wrapped in _rm cleanup, so a replace failure (e.g. a Windows sharing violation when
      dest is held open by a running crack) leaked the verified temp (many MiB). Now wrapped.

Pure logic + fakes: no hardware, network, GPS, or real serial port is touched.
"""
from __future__ import annotations

import os
import threading

import pytest

# ── S1: a 0,0 "Null Island" GPS fix is treated as no-position, not a valid stamped fix ──

def test_parse_nmea_rejects_null_island_gga():
    from src.core import wardrive

    fix = wardrive.parse_nmea("$GPGGA,123519,0000.0000,N,00000.0000,E,1,08,0.9,10.0,M,,,,")
    assert fix is not None
    assert fix.has_fix is False, "a fix reported at exactly 0,0 must NOT be a valid position"


def test_parse_nmea_rejects_null_island_rmc():
    from src.core import wardrive

    fix = wardrive.parse_nmea("$GPRMC,123519,A,0000.0000,N,00000.0000,E,0.0,0.0,230394,,")
    assert fix is not None
    assert fix.has_fix is False, "an RMC 'A' status at exactly 0,0 must NOT be a valid position"


def test_parse_nmea_keeps_real_fix():
    from src.core import wardrive

    fix = wardrive.parse_nmea("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,,,,")
    assert fix is not None and fix.has_fix is True
    assert round(fix.lat, 3) == 48.117 and round(fix.lon, 3) == 11.517


def test_parse_nmea_keeps_equator_fix_only_one_coord_zero():
    # A genuine equator (lat 0) OR prime-meridian (lon 0) fix has only ONE coordinate at 0.0, never
    # both — the guard must NOT reject it (that would drop legitimate positions).
    from src.core import wardrive

    fix = wardrive.parse_nmea("$GPGGA,123519,0000.0000,N,01131.000,E,1,08,0.9,10.0,M,,,,")
    assert fix is not None and fix.has_fix is True
    assert fix.lat == 0.0 and round(fix.lon, 3) == 11.517


# ── S2: detach() holds the controller RLock across the ENTIRE teardown (no re-attach race) ──

def test_detach_holds_lock_through_entire_teardown(monkeypatch):
    import src.core.nodes_controller as nc

    monkeypatch.setattr(nc.node_provision, "persist_rx_state", lambda *a, **k: None)

    seen = {"close": None, "remove": None}

    def _lock_is_held(ctl) -> bool:
        # From a SEPARATE thread, try to grab the controller's RLock non-blocking. An RLock held by
        # the detaching thread cannot be acquired by another thread, so failure to acquire == held.
        got: list = []
        t = threading.Thread(target=lambda: got.append(ctl._lock.acquire(blocking=False)))
        t.start()
        t.join()
        acquired = got[0]
        if acquired:
            ctl._lock.release()
        return not acquired

    class _FakeLink:
        port = "node:5"

        def on_rx_advance(self, _cb):
            pass

        def close(self):
            pass

    class _FakeDM:
        # Both teardown steps probe the lock; pre-fix, detach released it right after the pop,
        # so these would run UNLOCKED (a concurrent attach could slip in).
        def close_connection(self, _port, owner=None):
            seen["close"] = _lock_is_held(ctl)

        def remove_device(self, _port):
            seen["remove"] = _lock_is_held(ctl)

    ctl = nc.NodesController(_FakeDM(), vault_getter=lambda: object())
    ctl._links[5] = _FakeLink()
    ctl._gateway_owned[5] = "COM_GW"   # so the gateway-release close_connection path is exercised

    assert ctl.detach(5) is True
    assert seen["close"] is True, "lock released before gateway close — re-attach race window"
    assert seen["remove"] is True, "lock was released before remove_device — re-attach race window"


# ── S3: clear_failed_update sweeps .new orphans even when the install path has glob metachars ──

def test_clear_failed_update_sweeps_new_with_glob_metachars_in_path(tmp_path):
    from src.core import self_update

    d = tmp_path / "cyber[portable]"          # legal folder name; '[ ]' are glob metacharacters
    d.mkdir()
    exe = d / "cyber-controller.exe"
    exe.write_text("x", encoding="ascii")
    orphan = d / "cyber-controller-v2-windows-x64.exe.new"
    orphan.write_text("staged verified binary", encoding="ascii")

    self_update.clear_failed_update(cur_exe=str(exe))
    assert not orphan.exists(), "the orphaned .new must be swept even with [ ] in the install path"


# ── S4: a final os.replace failure cleans up the verified temp instead of leaking it ──────────────

def test_download_wordlist_cleans_temp_when_final_replace_fails(monkeypatch, tmp_path):
    import src.core.wordlist_manager as wm

    spec = wm.WordlistSpec(
        id="test-x", name="test", description="d",
        url="http://example.invalid/test.txt", size_bytes=4,
    )  # compressed="" (no gz branch), sha256="" (size-only)

    class _CM:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(wm.urllib.request, "urlopen", lambda *a, **k: _CM())
    monkeypatch.setattr(wm, "_stream_capped", lambda _resp, out, _spec: out.write(b"data"))
    monkeypatch.setattr(wm, "verify_file", lambda _p, _s: (True, "ok"))

    def _boom(_src, _dst):
        raise OSError("sharing violation: dest is open in another program")

    monkeypatch.setattr(wm.os, "replace", _boom)

    with pytest.raises(RuntimeError):
        wm.download_wordlist(spec, directory=str(tmp_path), force=True)

    leftovers = [p for p in os.listdir(tmp_path) if p.startswith("cc-wl-")]
    assert leftovers == [], f"verified temp leaked when the atomic install failed: {leftovers}"
