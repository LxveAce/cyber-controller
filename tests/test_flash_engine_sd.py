"""FlashEngine SD-image flow (Pwnagotchi / RaspyJack / Kali ARM).

Wires the previously-dead ``sd_backend`` Pi pipeline (``discover_images`` / ``flash_sd``) into a real,
device-driven engine entry point. Before this, the three ``backend:"sd"`` firmware profiles were
selectable in the Flash tab but hit an inert ``_flash_sd`` stub that pointed at an "SD flow" which did
not exist — advertised but unflashable.

No hardware, no network: ``sd_backend`` is stubbed so the tests exercise the ENGINE's delegation, the
confirmation gate, and rc/exception handling — never a real SD write.
"""

from __future__ import annotations

import pytest

from src.core.flash_engine import FlashEngine, FirmwareProfile, FlashStatus

sd_backend = pytest.importorskip("src.core.backends.sd_backend")


_ASSET = {"name": "pwnagotchi.img.xz",
          "url": "https://github.com/jayofelony/pwnagotchi/releases/download/v2/pwnagotchi.img.xz",
          "size": 10}


def test_flash_sd_image_delegates_to_backend_with_confirm(monkeypatch):
    rec = {}

    def fake_flash_sd(profile_id, asset, device, on_line, on_progress=None,
                      confirmed=False, verify=True):
        rec.update(profile_id=profile_id, asset=asset, device=device,
                   confirmed=confirmed, verify=verify)
        on_line("[sd] downloading...")
        if on_progress:
            on_progress(0.5)
        return 0

    monkeypatch.setattr(sd_backend, "flash_sd", fake_flash_sd)

    lines = []
    eng = FlashEngine()
    ok = eng.flash_sd_image("pwnagotchi", _ASSET, "/dev/sdb",
                            lambda pct, msg: lines.append((pct, msg)), confirmed=True)
    assert ok is True
    # The engine delegated to the REAL sd_backend pipeline with the safety flag forced on.
    assert rec == {"profile_id": "pwnagotchi", "asset": _ASSET, "device": "/dev/sdb",
                   "confirmed": True, "verify": True}
    # on_progress(0.5) -> 50%, and the last log line is bridged onto the (percent, message) cb.
    assert (50, "[sd] downloading...") in lines
    assert eng.status is FlashStatus.DONE


def test_flash_sd_image_requires_confirmation(monkeypatch):
    called = {"flash": False}
    monkeypatch.setattr(sd_backend, "flash_sd",
                        lambda *a, **k: called.__setitem__("flash", True) or 0)

    eng = FlashEngine()
    with pytest.raises(ValueError):
        eng.flash_sd_image("pwnagotchi", _ASSET, "/dev/sdb", confirmed=False)
    # No confirmation -> the destructive backend pipeline is NEVER entered (no write attempted).
    assert called["flash"] is False


def test_flash_sd_image_returns_false_on_nonzero_rc(monkeypatch):
    monkeypatch.setattr(sd_backend, "flash_sd", lambda *a, **k: 1)
    eng = FlashEngine()
    assert eng.flash_sd_image("raspyjack", _ASSET, "/dev/sdb", confirmed=True) is False
    assert eng.status is FlashStatus.ERROR


def test_flash_sd_image_false_on_backend_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no such device")

    monkeypatch.setattr(sd_backend, "flash_sd", boom)
    eng = FlashEngine()
    # A backend blow-up is caught and reported as failure, never a false success.
    assert eng.flash_sd_image("kali-arm", _ASSET, "/dev/sdb", confirmed=True) is False
    assert eng.status is FlashStatus.ERROR


def test_discover_sd_images_delegates(monkeypatch):
    rec = {}

    def fake_discover(profile_id, on_line):
        rec["profile_id"] = profile_id
        on_line("[sd] querying...")
        return [_ASSET]

    monkeypatch.setattr(sd_backend, "discover_images", fake_discover)

    lines = []
    eng = FlashEngine()
    out = eng.discover_sd_images("pwnagotchi", lambda pct, msg: lines.append(msg))
    assert out == [_ASSET]
    assert rec["profile_id"] == "pwnagotchi"
    assert "[sd] querying..." in lines


def test_sd_registry_handler_is_honest_and_points_to_real_method(monkeypatch):
    # The serial dispatch handler for backend:"sd" must NOT claim a flow that doesn't exist; it must
    # point at the real, wired entry point (flash_sd_image) — and never report success or write.
    def must_not_write(*a, **k):
        raise AssertionError("the serial SD handler must never attempt a destructive write")

    monkeypatch.setattr(sd_backend, "flash_sd", must_not_write)

    lines = []
    prof = FirmwareProfile(name="Pwnagotchi", id="pwnagotchi", backend="sd")
    ok = FlashEngine()._flash_sd("COM5", prof, lambda pct, msg: lines.append(msg))
    assert ok is False
    blob = " ".join(lines)
    assert "flash_sd_image" in blob            # references the REAL entry point
    assert "SD flow which calls" not in blob   # the old nonexistent-flow wording is gone


def test_sd_backends_registered():
    eng = FlashEngine()
    assert eng._backends["sd"] == eng._flash_sd
    assert eng._backends["sd-image"] == eng._flash_sd
