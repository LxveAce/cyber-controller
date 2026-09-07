from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core import health_monitor as hm

PRIMARY = SimpleNamespace(percent=75.0, used=3 * 1024**3, total=4 * 1024**3)
ROOT = SimpleNamespace(percent=25.0, used=1024**3, total=4 * 1024**3)


@pytest.fixture
def health_inputs(monkeypatch):
    inputs = SimpleNamespace(
        cpu=Mock(return_value=(42.5, False)),
        memory=Mock(return_value=SimpleNamespace(
            percent=37.5, used=1536 * 1024, total=20 * 1024**2,
        )),
        battery=Mock(return_value=SimpleNamespace(percent=60.0)),
        now=Mock(return_value=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)),
    )
    monkeypatch.setattr(hm, "_cpu_sample", inputs.cpu)
    monkeypatch.setattr(hm, "psutil", SimpleNamespace(
        virtual_memory=inputs.memory, sensors_battery=inputs.battery,
    ))
    monkeypatch.setattr(hm, "datetime", SimpleNamespace(now=inputs.now))
    return inputs


def _disk_probe(outcomes, wrapped):
    calls = []

    def probe(path):
        calls.append(path)
        value = outcomes[path]
        if isinstance(value, BaseException):
            raise value
        return value

    if wrapped:
        probe.__wrapped__ = object()
    hm.psutil.disk_usage = probe
    return calls


@pytest.mark.parametrize("wrapped", [False, True])
def test_primary_disk_result_needs_only_one_probe(health_inputs, wrapped):
    calls = _disk_probe({"C:\\": PRIMARY, "/": ROOT}, wrapped)
    health = hm.HealthMonitor.get_system_health()
    assert health["disk_percent"] == 75.0
    assert health["disk_used_gb"] == 3.0
    assert health["disk_total_gb"] == 4.0
    assert calls == ["C:\\"]


@pytest.mark.parametrize("wrapped", [False, True])
def test_failed_primary_uses_root_once(health_inputs, wrapped):
    calls = _disk_probe({"C:\\": OSError("primary unavailable"), "/": ROOT}, wrapped)
    health = hm.HealthMonitor.get_system_health()
    assert health["disk_percent"] == 25.0
    assert health["disk_used_gb"] == 1.0
    assert health["disk_total_gb"] == 4.0
    assert calls == ["C:\\", "/"]


@pytest.mark.parametrize("wrapped", [False, True])
def test_both_disk_errors_leave_other_health_available(health_inputs, wrapped):
    calls = _disk_probe({"C:\\": OSError("primary"), "/": OSError("root")}, wrapped)
    health = hm.HealthMonitor.get_system_health()
    assert health["disk_percent"] == health["disk_used_gb"] == health["disk_total_gb"] == 0.0
    assert health["cpu_percent"] == 42.5
    assert health["battery_percent"] == 60.0
    assert health["timestamp"] == "2026-01-02T03:04:05+00:00"
    assert calls == ["C:\\", "/"]


def test_primary_success_never_probes_unavailable_root(health_inputs):
    calls = _disk_probe({"C:\\": PRIMARY, "/": OSError("root unavailable")}, False)
    assert hm.HealthMonitor.get_system_health()["disk_percent"] == 75.0
    assert calls == ["C:\\"]


@pytest.mark.parametrize("cpu,battery", [((0.0, False), None), ((81.5, True), 0.0)])
def test_health_schema_freshness_battery_and_time_are_preserved(health_inputs, cpu, battery):
    health_inputs.cpu.return_value = cpu
    health_inputs.battery.return_value = (
        None if battery is None else SimpleNamespace(percent=battery)
    )
    _disk_probe({"C:\\": PRIMARY, "/": ROOT}, False)
    assert hm.HealthMonitor.get_system_health() == {
        "cpu_percent": cpu[0], "cpu_stale": cpu[1],
        "memory_percent": 37.5, "memory_used_mb": 2, "memory_total_mb": 20,
        "disk_percent": 75.0, "disk_used_gb": 3.0, "disk_total_gb": 4.0,
        "battery_percent": battery, "gps_fix": False,
        "timestamp": "2026-01-02T03:04:05+00:00",
    }
    health_inputs.cpu.assert_called_once_with()
    health_inputs.memory.assert_called_once_with()
    health_inputs.battery.assert_called_once_with()
    health_inputs.now.assert_called_once_with(timezone.utc)


@pytest.mark.parametrize("error", [KeyboardInterrupt("stop"), SystemExit("stop")])
def test_primary_control_exception_is_not_swallowed(health_inputs, error):
    _disk_probe({"C:\\": error, "/": ROOT}, False)
    with pytest.raises(type(error)) as raised:
        hm.HealthMonitor.get_system_health()
    assert raised.value is error
