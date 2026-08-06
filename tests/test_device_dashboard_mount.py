"""Shell-side guard for the DEVICE > Dashboard front door (the reform landing).

test_device_dashboard.py checks the STANDALONE widget; this guards the MOUNTED path: the real
CyberControllerWindow's `_device_dashboard` is the first DEVICE sub-tab AND carries the full
REFORM-DENSITY-SPEC device field set. The Dashboard composes two legitimate ways (density spec:
prefer re-parenting, else EXTRACT the readout — never drop a FIELD):
  * RE-HOMED by reference — most widgets (device list, gauges, serial terminal, BlueJammer/Mesh
    panels) are the LIVE host widgets re-parented into the Dashboard subtree.
  * FRESH MIRROR — the Selected Device card renders its own `_sd_*` widgets from the host's OWN
    formatters (arm_lamp_render / _format_health / _current_capabilities / _telemetry_line /
    _alert_line / _snapshot_line); re-homing those inline-styled readouts couldn't be made readable.
    Same data, no field dropped.
So the guard checks the right invariant per widget: re-homed ones must be under the Dashboard by
reference; mirrored fields must have their fresh Dashboard widget present + mounted. Either way, a
field that stops reaching the front door (attr rename, a dropped mirror) is a loud failure.

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


# RE-HOMED by reference (host attr on the window, widget attr, label): the LIVE host widget must be
# re-parented into the Dashboard subtree (density-critical controls/readouts + safety items).
_REHOMED = [
    ("_device_tab", "_device_list",     "device list"),
    ("_device_tab", "_btn_connect",     "connect button"),
    ("_device_tab", "_firmware_combo",  "firmware combo"),
    ("_health_tab", "_cpu_gauge",       "CPU gauge"),
    ("_health_tab", "_ram_gauge",       "RAM gauge"),
    ("_health_tab", "_disk_gauge",      "disk gauge"),
    ("_health_tab", "_batt_gauge",      "battery gauge"),
    ("_health_tab", "_gps_status",      "GPS status"),
    ("_device_tab", "_terminal",        "serial terminal"),
    ("_device_tab", "_cmd_input",       "command input"),
    ("_device_tab", "_btn_send",        "send button"),
    ("_device_tab", "_bj_panel",        "BlueJammer panel"),
    ("_device_tab", "_mesh_panel",      "Meshtastic panel"),
]

# FRESH MIRROR (Dashboard attr, label): the Selected Device card renders these from the host's own
# formatters (not re-homed). A field is surfaced iff its fresh Dashboard widget exists + is mounted.
# `_sd_arm` is the ARM/SAFE lamp (safety) — it MUST have a home on the front door.
_MIRRORED = [
    ("_sd_arm",    "ARM/SAFE lamp"),
    ("_sd_health", "health chip"),
    ("_sd_caps",   "capabilities readout"),
    ("_sd_telem",  "telemetry readout"),
    ("_sd_alert",  "alert readout"),
    ("_sd_snap",   "airspace snapshot"),
]


def test_dashboard_is_the_first_device_subtab(win):
    # the mounted front door leads the DEVICE surface (what the app lands on).
    assert win._rig_surface.widget(0) is win._device_dashboard


@pytest.mark.parametrize("host_attr,widget_attr,label", _REHOMED,
                         ids=[c[2] for c in _REHOMED])
def test_rehomed_widget_is_live_and_under_dashboard(win, host_attr, widget_attr, label):
    host = getattr(win, host_attr)
    widget = getattr(host, widget_attr, None)
    assert widget is not None, f"host lost {widget_attr} — density source gone ({label})"
    assert win._device_dashboard.isAncestorOf(widget), (
        f"{label} exists but is NOT under the mounted Dashboard — front door dropped it")


@pytest.mark.parametrize("dash_attr,label", _MIRRORED, ids=[c[1] for c in _MIRRORED])
def test_selected_device_field_is_surfaced_fresh(win, dash_attr, label):
    # Selected Device fields are mirrored (fresh widgets), not re-homed — guard the FIELD: its fresh
    # Dashboard widget must exist AND be mounted on the front door (a dropped mirror = lost field).
    widget = getattr(win._device_dashboard, dash_attr, None)
    assert widget is not None, f"Selected Device mirror {dash_attr} missing — dropped ({label})"
    assert win._device_dashboard.isAncestorOf(widget), (
        f"{label} mirror exists but is NOT under the mounted Dashboard — front door dropped it")


def test_bluejammer_stop_is_mounted_and_ungated(win):
    # owner directive: the BlueJammer STOP stays UNGATED. Present-in-panel is not enough — a STOP
    # control must be reachable (enabled) inside the re-homed panel on the front door.
    bj = getattr(win._device_tab, "_bj_panel", None)
    assert bj is not None and win._device_dashboard.isAncestorOf(bj)
    stops = [b for b in bj.findChildren(QPushButton)
             if "stop" in (b.text() or "").lower() or "stop" in (b.objectName() or "").lower()]
    assert stops, "no STOP-labelled control inside the re-homed BlueJammer panel"
    assert any(b.isEnabled() for b in stops), "every BlueJammer STOP control is disabled (gated)"
