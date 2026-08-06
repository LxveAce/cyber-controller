"""Shell-side guard for the DEVICE > Dashboard front door (the reform landing).

test_device_dashboard.py checks the STANDALONE widget re-homes a spot-check of widgets. This guards
the MOUNTED path: when the real CyberControllerWindow builds, its `_device_dashboard` is the first
DEVICE sub-tab AND carries the FULL REFORM-DENSITY-SPEC device field set (every readout, the serial
terminal, and the three safety items) re-homed as live widgets. The re-composition wires by
`getattr(host, "_attr", None)`, so a future rename of a DeviceTab/HealthTab attr would SILENTLY drop
a field from the front door (the "a field went missing" regression). This makes that loud.

The BlueJammer STOP is asserted present AND enabled — owner directive is that STOP stays ungated, so
"the panel is mounted" is not enough; the STOP control must be reachable.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication, QPushButton  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _no_blocking_sd_probe(monkeypatch):
    """SoftwareTab.__init__ shells out to PowerShell Get-Disk on Windows; stub it so building the
    window is instant + offline (this suite never touches SD detection)."""
    import src.core.backends.sd_backend as sd
    monkeypatch.setattr(sd, "detect_sd_cards", lambda *a, **k: [])


def _make_window():
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt.main_window import CyberControllerWindow
    bus = EventBus()
    return CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))


def _quiesce(win) -> None:
    try:
        win._health.stop()
    except Exception:  # noqa: BLE001
        pass
    for timer in win.findChildren(QTimer):
        timer.stop()


@pytest.fixture
def win(qapp):
    w = _make_window()
    _quiesce(w)
    # Focus the Dashboard so it's realized (showEvent parents the re-homed leaves).
    w._show_subtab(w._rig_surface, w._device_dashboard)
    qapp.processEvents()
    yield w
    try:
        w.close()
    except Exception:  # noqa: BLE001
        pass
    w.deleteLater()
    qapp.processEvents()


# (host attr on the window, widget attr, human label). The density-critical device readouts +
# controls + safety items the front door carries (REFORM-DENSITY-SPEC Dashboard field set).
_DENSITY = [
    ("_device_tab", "_device_list",     "device list"),
    ("_device_tab", "_btn_connect",     "connect button"),
    ("_device_tab", "_firmware_combo",  "firmware combo"),
    ("_health_tab", "_cpu_gauge",       "CPU gauge"),
    ("_health_tab", "_ram_gauge",       "RAM gauge"),
    ("_health_tab", "_disk_gauge",      "disk gauge"),
    ("_health_tab", "_batt_gauge",      "battery gauge"),
    ("_health_tab", "_gps_status",      "GPS status"),
    ("_device_tab", "_arm_label",       "ARM/SAFE lamp"),
    ("_device_tab", "_health_label",    "health chip"),
    ("_device_tab", "_caps_label",      "capabilities readout"),
    ("_device_tab", "_telemetry_label", "telemetry readout"),
    ("_device_tab", "_alert_label",     "alert readout"),
    ("_device_tab", "_snapshot_label",  "airspace snapshot"),
    ("_device_tab", "_terminal",        "serial terminal"),
    ("_device_tab", "_cmd_input",       "command input"),
    ("_device_tab", "_btn_send",        "send button"),
    ("_device_tab", "_bj_panel",        "BlueJammer panel"),
    ("_device_tab", "_mesh_panel",      "Meshtastic panel"),
]


def test_dashboard_is_the_first_device_subtab(win):
    # the mounted front door leads the DEVICE surface (what the app lands on).
    assert win._rig_surface.widget(0) is win._device_dashboard


@pytest.mark.parametrize("host_attr,widget_attr,label", _DENSITY,
                         ids=[c[2] for c in _DENSITY])
def test_density_widget_is_live_and_rehomed(win, host_attr, widget_attr, label):
    host = getattr(win, host_attr)
    widget = getattr(host, widget_attr, None)
    assert widget is not None, f"host lost {widget_attr} — density source gone ({label})"
    assert win._device_dashboard.isAncestorOf(widget), (
        f"{label} exists but is NOT under the mounted Dashboard — front door dropped it")


def test_bluejammer_stop_is_mounted_and_ungated(win):
    # owner directive: the BlueJammer STOP stays UNGATED. Present-in-panel is not enough — a STOP
    # control must be reachable (enabled) inside the re-homed panel on the front door.
    bj = getattr(win._device_tab, "_bj_panel", None)
    assert bj is not None and win._device_dashboard.isAncestorOf(bj)
    stops = [b for b in bj.findChildren(QPushButton)
             if "stop" in (b.text() or "").lower() or "stop" in (b.objectName() or "").lower()]
    assert stops, "no STOP-labelled control inside the re-homed BlueJammer panel"
    assert any(b.isEnabled() for b in stops), "every BlueJammer STOP control is disabled (gated)"
