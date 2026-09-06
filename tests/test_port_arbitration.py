"""Connection and flash races exercised without serial hardware."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace

import pytest

from src.core.port_arbitration import (
    DeviceIdentity,
    IdentityProvenance,
    InvalidLease,
    PortArbiter,
    PortBusy,
    PortUnavailable,
    canonical_port,
)

DEVICE = DeviceIdentity(
    "usb-device-1", "example-cli", "serial", "text-cli", IdentityProvenance.DETECTED,
)


def bind(arbiter, *, port="COM3", device=DEVICE, connection=None):
    lease = arbiter.reserve(port)
    try:
        with lease.access() as access:
            return access.bind(device, connection or object(), object())
    finally:
        lease.close()


@pytest.mark.parametrize("port", ["COM3", "com3", r"\\.\COM3", r"\\.\com03"])
def test_windows_aliases_share_reservation(port):
    arbiter = PortArbiter()
    lease = arbiter.reserve(port)
    try:
        with pytest.raises(PortBusy):
            arbiter.reserve("COM3")
    finally:
        lease.close()


def test_unix_case_is_preserved():
    arbiter = PortArbiter()
    leases = [arbiter.reserve(p) for p in ("/dev/ttyUSB0", "/dev/ttyusb0")]
    assert canonical_port("/dev/ttyUSB0") == "/dev/ttyUSB0"
    for lease in leases:
        lease.close()


@pytest.mark.parametrize("port", [None, 4, "", " COM3", "COM3\n", "COM\x003", "x" * 513])
def test_invalid_port_rejected(port):
    with pytest.raises(ValueError):
        PortArbiter().reserve(port)


@pytest.mark.parametrize("maximum", [None, True, 0, -1, 2.5])
def test_capacity_is_positive_finite_integer(maximum):
    with pytest.raises(ValueError):
        PortArbiter(max_ports=maximum)


def test_empty_reservations_reclaim_capacity_and_old_tokens_never_reappear():
    arbiter = PortArbiter(max_ports=1)
    old = bind(arbiter)
    with pytest.raises(PortUnavailable):
        arbiter.reserve("COM4")
    lease = arbiter.reserve("COM3")
    with lease.access() as access:
        assert access.unbind() == old
    lease.close()
    other = arbiter.reserve("COM4")
    other.close()
    new = bind(arbiter)
    assert old.token != new.token
    assert old.token.generation < new.token.generation


def test_snapshot_is_complete_and_identity_immutable():
    arbiter = PortArbiter()
    connection = object()
    original = bind(arbiter, connection=connection)
    lease = arbiter.reserve("COM3")
    with lease.access() as access:
        snapshot = access.snapshot()
        assert snapshot is original
        assert snapshot.connection is connection
        assert snapshot.device is DEVICE
        assert snapshot.driver is not None
        with pytest.raises(FrozenInstanceError):
            snapshot.device.firmware = "different"
        with pytest.raises(FrozenInstanceError):
            snapshot.connection = object()
    lease.close()


def test_same_object_reopen_and_identity_change_each_invalidate_queued_token():
    arbiter = PortArbiter()
    connection = object()
    initial = bind(arbiter, connection=connection)
    lease = arbiter.reserve("COM3")
    with lease.access() as access:
        assert access.matches(initial.token)
        reopened = access.bind(DEVICE, connection, initial.driver)
        assert not access.matches(initial.token)
        changes = (
            {"device_id": "replacement"}, {"firmware": "different"},
            {"protocol": "stream"},
            {"firmware_forced": True, "provenance": IdentityProvenance.OPERATOR},
            {"driver_type": "stream"},
        )
        previous = reopened
        for change in changes:
            current = access.bind(replace(DEVICE, **change), connection, object())
            assert not access.matches(previous.token)
            assert access.matches(current.token)
            previous = current
    lease.close()


def test_binding_tokens_are_specific_to_registry():
    first = bind(PortArbiter())
    second = bind(PortArbiter())
    assert first.token != second.token


def test_failed_close_leaves_old_binding_invalid_with_reservation_held():
    arbiter = PortArbiter()
    binding = bind(arbiter)
    lease = arbiter.reserve("COM3")
    with lease.access() as access:
        previous = access.unbind()
        assert previous is binding
        with pytest.raises(OSError):
            raise OSError("simulated transport close failure")
        assert not access.matches(binding.token)
        with pytest.raises(PortBusy):
            arbiter.reserve("COM3")
    lease.close()
    assert bind(arbiter).token != binding.token


def test_expired_access_and_nested_access_fail_without_deadlock():
    arbiter = PortArbiter()
    lease = arbiter.reserve("COM3")
    with lease.access() as first:
        with pytest.raises(PortBusy):
            arbiter.reserve("COM3")
        with pytest.raises(InvalidLease), lease.access():
            pass
    with lease.access() as second:
        with pytest.raises(InvalidLease):
            first.snapshot()
        second.bind(DEVICE, object(), object())
    lease.close()
    with pytest.raises(InvalidLease):
        second.snapshot()


def test_active_access_cannot_cross_threads_or_be_released_under_io():
    arbiter = PortArbiter()
    lease = arbiter.reserve("COM3")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with lease.access() as access:
            with pytest.raises(InvalidLease):
                pool.submit(access.snapshot).result(timeout=2)
            with pytest.raises(InvalidLease):
                pool.submit(lease.close).result(timeout=2)
            with pytest.raises(PortBusy):
                pool.submit(arbiter.reserve, "COM3").result(timeout=2)
            assert access.snapshot() is None
    lease.close()


def test_transfer_reserved_flash_to_worker_and_launch_failure_cleanup():
    arbiter = PortArbiter()
    original = bind(arbiter)
    flash = arbiter.reserve("COM3")  # before worker launch/connection close
    with pytest.raises(PortBusy):
        arbiter.reserve("COM3")

    def worker():
        try:
            with flash.access() as access:
                assert access.snapshot() is original
                access.unbind()
                return access.bind(DEVICE, object(), object())
        finally:
            flash.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        current = pool.submit(worker).result(timeout=2)
    assert current.token != original.token
    failed_launch = arbiter.reserve("COM3")
    failed_launch.close()
    next_lease = arbiter.reserve("COM3")
    failed_launch.close()  # delayed duplicate cleanup cannot release the new lease
    with pytest.raises(PortBusy):
        arbiter.reserve("COM3")
    next_lease.close()


def test_simulated_write_holds_reservation_until_completion():
    arbiter = PortArbiter()
    bind(arbiter)
    entered = threading.Event()
    finish = threading.Event()
    wire = []

    def writer():
        lease = arbiter.reserve("COM3")
        try:
            with lease.access():
                wire.append("write-begin")
                entered.set()
                assert finish.wait(2)
                wire.append("write-end")
        finally:
            lease.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer)
        try:
            assert entered.wait(2)
            with pytest.raises(PortBusy):
                arbiter.reserve("COM3")
            independent = arbiter.reserve("COM4")
            independent.close()
        finally:
            finish.set()
        future.result(timeout=2)
    flash = arbiter.reserve("COM3")
    with flash.access() as access:
        access.unbind()
        wire.append("flash-start")
    flash.close()
    assert wire == ["write-begin", "write-end", "flash-start"]


def test_simultaneous_reservation_has_one_winner():
    arbiter = PortArbiter()
    start = threading.Barrier(8)

    def contender():
        start.wait(2)
        try:
            return arbiter.reserve("COM3")
        except PortBusy:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(contender) for _ in range(8)]
        winners = [lease for f in futures if (lease := f.result(timeout=3)) is not None]
    assert len(winners) == 1
    winners[0].close()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_context_unwinds_access_for_worker_finally_cleanup(failure):
    arbiter = PortArbiter(max_ports=1)
    lease = arbiter.reserve("COM3")
    with pytest.raises(failure), lease.access():
        raise failure()
    lease.close()
    arbiter.reserve("COM4").close()
