"""Identity and access-lifetime regressions using only synthetic transports."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from enum import Enum

import pytest

import src.core.port_arbitration as arbitration
from src.core.port_arbitration import (
    DeviceIdentity,
    IdentityProvenance,
    InvalidLease,
    PortArbiter,
    PortBusy,
    PortUnavailable,
)

DEVICE = DeviceIdentity(
    "usb-device-1", "example-cli", "serial", "text-cli", IdentityProvenance.DETECTED,
)
IDENTITY_LIMITS = [("device_id", 512), ("firmware", 128), ("protocol", 128), ("driver_type", 64)]


class StringSubclass(str):
    pass


class OtherProvenance(str, Enum):
    DETECTED = "detected"


@pytest.mark.parametrize("field,limit", IDENTITY_LIMITS)
@pytest.mark.parametrize("character", ["x", "é"])
def test_identity_preserves_exact_encoded_boundary_and_rejects_overflow(field, limit, character):
    value = character * (limit // len(character.encode("utf-8")))
    assert len(value.encode("utf-8")) == limit
    identity = replace(DEVICE, **{field: value})
    assert getattr(identity, field) == value
    with pytest.raises(ValueError):
        replace(DEVICE, **{field: value + character})


@pytest.mark.parametrize("field,limit", IDENTITY_LIMITS)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param([], id="mutable-list"),
        pytest.param({}, id="mutable-dict"),
        pytest.param(None, id="none"),
        pytest.param(1, id="integer"),
        pytest.param(True, id="boolean"),
        pytest.param(StringSubclass("valid-looking"), id="string-subclass"),
        pytest.param("", id="empty"),
        pytest.param(" device", id="leading-whitespace"),
        pytest.param("device ", id="trailing-whitespace"),
        pytest.param("dev\x00ice", id="nul"),
        pytest.param("dev\nice", id="embedded-newline"),
        pytest.param("dev\u200bice", id="nonprinting-unicode"),
        pytest.param("dev\ud800ice", id="unpaired-surrogate"),
    ],
)
def test_identity_rejects_mutable_nonexact_and_nonprintable_fields(field, limit, value):
    with pytest.raises(ValueError):
        replace(DEVICE, **{field: value})


@pytest.mark.parametrize("provenance", list(IdentityProvenance))
def test_identity_accepts_explicit_provenance_without_implicit_forcing(provenance):
    identity = replace(DEVICE, provenance=provenance)
    assert identity.provenance is provenance
    assert identity.firmware_forced is False


@pytest.mark.parametrize("provenance", [None, "detected", "operator", OtherProvenance.DETECTED, 1])
def test_provenance_is_required_and_cannot_be_a_lookalike(provenance):
    with pytest.raises(ValueError):
        replace(DEVICE, provenance=provenance)


def test_provenance_has_no_implicit_default():
    with pytest.raises(TypeError):
        DeviceIdentity("usb-device-1", "example-cli", "serial", "text-cli")


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_firmware_forced_requires_an_exact_boolean(value):
    with pytest.raises(ValueError):
        replace(DEVICE, provenance=IdentityProvenance.OPERATOR, firmware_forced=value)


@pytest.mark.parametrize("provenance", [IdentityProvenance.DETECTED, IdentityProvenance.PROFILE])
def test_firmware_forced_cannot_claim_nonoperator_provenance(provenance):
    with pytest.raises(ValueError):
        replace(DEVICE, provenance=provenance, firmware_forced=True)


def test_valid_operator_override_is_immutable_and_bad_replacement_leaves_binding_intact():
    identity = replace(DEVICE, provenance=IdentityProvenance.OPERATOR, firmware_forced=True)
    arbiter = PortArbiter()
    lease = arbiter.reserve("COM3")
    try:
        with lease.access() as access:
            original = access.bind(identity, object(), object())
            for field, value in (
                ("provenance", IdentityProvenance.DETECTED),
                ("firmware_forced", False),
                ("device_id", "replacement-device"),
            ):
                with pytest.raises(FrozenInstanceError):
                    setattr(identity, field, value)
            with pytest.raises(ValueError):
                access.bind(replace(identity, firmware=[]), object(), object())
            assert access.snapshot() is original
            assert access.matches(original.token)
            assert original.device.firmware_forced is True
            assert original.device.provenance is IdentityProvenance.OPERATOR
    finally:
        lease.close()


def test_unbound_generation_remains_observable_and_empty_unbind_is_not_an_event():
    lease = PortArbiter().reserve("COM3")
    try:
        with lease.access() as access:
            assert access.generation == 0
            assert access.unbind() is None
            assert access.generation == 0
            original = access.bind(DEVICE, object(), object())
            assert access.generation == original.token.generation
            assert access.unbind() is original
            invalidation_epoch = access.generation
            assert invalidation_epoch > original.token.generation
            assert access.snapshot() is None
            assert not access.matches(original.token)
            assert access.unbind() is None
            assert access.generation == invalidation_epoch
            with pytest.raises(AttributeError):
                access.generation = 0
        with pytest.raises(InvalidLease):
            _ = access.generation
        with lease.access() as next_access:
            assert next_access.generation == invalidation_epoch
            rebound = next_access.bind(DEVICE, object(), object())
            assert next_access.generation == rebound.token.generation > invalidation_epoch
            assert not next_access.matches(original.token)
    finally:
        lease.close()


def test_capacity_failure_is_distinct_from_busy_and_releases_only_after_unbinding():
    arbiter = PortArbiter(max_ports=1)
    lease = arbiter.reserve("COM3")
    with pytest.raises(PortBusy) as busy:
        arbiter.reserve("com03")
    assert not isinstance(busy.value, PortUnavailable)
    with pytest.raises(PortUnavailable) as unavailable:
        arbiter.reserve("COM4")
    assert not isinstance(unavailable.value, PortBusy)
    with lease.access() as access:
        binding = access.bind(DEVICE, object(), object())
    lease.close()
    with pytest.raises(PortUnavailable):
        arbiter.reserve("COM4")
    cleanup = arbiter.reserve("COM3")
    with cleanup.access() as access:
        assert access.unbind() is binding
        with pytest.raises(PortUnavailable):
            arbiter.reserve("COM4")
    cleanup.close()
    replacement = arbiter.reserve("COM4")
    replacement.close()


def test_foreign_exit_preserves_active_io_and_owner_can_finish_cleanup():
    arbiter = PortArbiter()
    lease = arbiter.reserve("COM3")
    context = lease.access()
    entered = threading.Barrier(2)
    writing = threading.Event()
    finish_write = threading.Event()
    writes = []

    class FakeConnection:
        def write(self, data):
            writing.set()
            assert finish_write.wait(5), "test did not release its synthetic write"
            writes.append(data)

    connection = FakeConnection()

    def owner():
        try:
            with context as access:
                binding = access.bind(DEVICE, connection, object())
                entered.wait(timeout=5)
                connection.write(b"synthetic-only")
                assert access.snapshot() is binding
                assert access.matches(binding.token)
                assert access.generation == binding.token.generation
            with pytest.raises(InvalidLease):
                access.snapshot()
        finally:
            lease.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        completed = pool.submit(owner)
        try:
            entered.wait(timeout=5)
            assert writing.wait(5)
            with pytest.raises(InvalidLease):
                context.__exit__(None, None, None)
            with pytest.raises(InvalidLease):
                lease.close()
            with pytest.raises(InvalidLease), lease.access():
                pass
            with pytest.raises(PortBusy):
                arbiter.reserve("COM3")
            assert not completed.done()
            assert writes == []
        finally:
            finish_write.set()
            completed.result(timeout=5)
    assert writes == [b"synthetic-only"]
    following = arbiter.reserve("COM3")
    following.close()


@pytest.mark.parametrize("new_reservation", [False, True])
def test_stale_context_cannot_release_newer_access_or_be_reentered(new_reservation):
    arbiter = PortArbiter()
    original_lease = arbiter.reserve("COM3")
    old_context = original_lease.access()
    with old_context as old_access:
        assert old_access.snapshot() is None
    if new_reservation:
        original_lease.close()
        lease = arbiter.reserve("COM3")
    else:
        lease = original_lease
    try:
        with lease.access() as current:
            binding = current.bind(DEVICE, object(), object())
            with pytest.raises(InvalidLease):
                old_context.__exit__(None, None, None)
            with pytest.raises(InvalidLease):
                old_context.__enter__()
            with pytest.raises(InvalidLease):
                old_access.snapshot()
            with pytest.raises(InvalidLease):
                _ = old_access.generation
            with pytest.raises(InvalidLease):
                lease.close()
            with pytest.raises(PortBusy):
                arbiter.reserve("COM3")
            assert current.snapshot() is binding
            assert current.matches(binding.token)
    finally:
        lease.close()


def test_equal_integer_thread_cookies_cannot_impersonate_the_owner(monkeypatch):
    class ThreadingView:
        def __getattr__(self, name):
            return getattr(threading, name)

        @staticmethod
        def get_ident():
            return 23

    # Limit the collision to this module; leave Python's thread machinery alone.
    view = ThreadingView()
    monkeypatch.setattr(arbitration, "threading", view)
    lease = PortArbiter().reserve("COM3")
    context = lease.access()
    try:
        with context as access, ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(view.get_ident).result(timeout=5) == view.get_ident()
            foreign_thread = pool.submit(view.current_thread).result(timeout=5)
            assert foreign_thread is not threading.current_thread()
            with pytest.raises(InvalidLease):
                pool.submit(access.snapshot).result(timeout=5)
            with pytest.raises(InvalidLease):
                pool.submit(context.__exit__, None, None, None).result(timeout=5)
            assert access.snapshot() is None
    finally:
        lease.close()


def test_port_identifier_cannot_supply_custom_string_equality():
    with pytest.raises(ValueError):
        arbitration.canonical_port(StringSubclass("COM3"))
