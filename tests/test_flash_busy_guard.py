"""Per-port concurrency guard: a second flash/backup/erase on a port that's already mid-operation must be
refused (two esptool processes on one UART can brick the board). Different ports stay independent so
multi-board flashing still works. All hermetic — the guard trips before any esptool is spawned.
"""

from __future__ import annotations

import pytest

from src.core.flash_engine import FlashEngine, _PortBusy
from src.core.resources import resource_path

_PROFILE = resource_path("src", "config", "profiles", "marauder.json")


def test_port_guard_rejects_second_same_port_but_allows_others():
    fe = FlashEngine()
    with fe._port_guard("COM9"):
        assert fe.is_port_busy("COM9") is True
        with pytest.raises(_PortBusy):
            with fe._port_guard("COM9"):
                pass
        with fe._port_guard("COM3"):  # a different port is free
            assert fe.is_port_busy("COM3") is True
    assert fe.is_port_busy("COM9") is False  # released on exit


def test_falsy_port_is_never_reserved():
    fe = FlashEngine()
    with fe._port_guard(""):
        assert fe.is_port_busy("") is False  # SD/UF2/blank paths aren't serial ports — not tracked


def test_flash_aborts_when_port_busy():
    fe = FlashEngine()
    profile = fe.load_profile(_PROFILE)
    msgs: list[str] = []
    with fe._port_guard("COM9"):  # hold the port as if another op owns it
        ok = fe.flash("COM9", profile, progress_callback=lambda _p, m: msgs.append(m))
    assert ok is False
    assert any("busy" in m.lower() for m in msgs), msgs


def test_backup_and_erase_abort_when_port_busy(tmp_path):
    fe = FlashEngine()
    with fe._port_guard("COM9"):
        bmsgs: list[str] = []
        emsgs: list[str] = []
        ok_b = fe.backup("COM9", tmp_path / "b.bin", progress_callback=lambda _p, m: bmsgs.append(m))
        ok_e = fe.erase("COM9", progress_callback=lambda _p, m: emsgs.append(m))
    assert ok_b is False and ok_e is False
    assert any("busy" in m.lower() for m in bmsgs), bmsgs
    assert any("busy" in m.lower() for m in emsgs), emsgs


# ── CYD "Detect board" now flows through the same guard (BUGHUNT-0708 #5) ────────────────────────
# The probe-flash used to run raw esptool with no reservation, so a Backup/Erase/Flash started during
# detect could put a second esptool on the same UART (a brick path). detect_cyd() now holds _port_guard.
def test_detect_cyd_reserves_the_port_during_the_probe(monkeypatch):
    fe = FlashEngine()
    seen: dict = {}

    def fake_detect(port, *, flash_probe=True, read_secs=6.0, progress=None):
        seen["busy_mid_probe"] = fe.is_port_busy(port)  # the guard must hold WHILE the probe runs
        return "RESULT"

    monkeypatch.setattr("src.core.cyd_detect.detect_cyd", fake_detect)
    out = fe.detect_cyd("COM9")
    assert out == "RESULT"
    assert seen["busy_mid_probe"] is True          # reserved during the probe-flash
    assert fe.is_port_busy("COM9") is False         # released afterwards


def test_detect_cyd_rejected_when_port_already_busy(monkeypatch):
    fe = FlashEngine()
    monkeypatch.setattr("src.core.cyd_detect.detect_cyd", lambda *a, **k: "RESULT")
    with fe._port_guard("COM9"):  # a flash/backup/erase already owns the port
        with pytest.raises(_PortBusy):
            fe.detect_cyd("COM9")


def test_detect_cyd_frees_the_port_even_when_the_probe_raises(monkeypatch):
    fe = FlashEngine()

    def boom(*a, **k):
        raise RuntimeError("probe flash failed")

    monkeypatch.setattr("src.core.cyd_detect.detect_cyd", boom)
    with pytest.raises(RuntimeError):
        fe.detect_cyd("COM9")
    assert fe.is_port_busy("COM9") is False  # released on the error path too — no stuck-busy port
