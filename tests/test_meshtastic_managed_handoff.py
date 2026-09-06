"""M1-M3 corrective witnesses: queued effects, coherent private state, retained outcomes."""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest
from test_meshtastic_bootstrap import Harness, bs, complete, cs, local, node, ok, text

from src.core.meshtastic_owner import MeshWireOwner, OrderedSerialBinding
from src.protocols import meshtastic_stream as ms


def until(h, condition):
    for _ in range(100):
        if condition():
            return
        h.owner.tick()
    pytest.fail("bounded inert control point was not reached")


def ready(h):
    h.begin()
    until(h, lambda: len(h.ids) == 1)
    h.feed(complete(h.ids[0]))
    until(h, lambda: len(h.ids) == 2)
    h.feed(local() + node() + complete(h.ids[1]))
    until(h, lambda: h.b.config_complete)


def successor(state):
    frames, token = [], object()
    owner = MeshWireOwner(
        OrderedSerialBinding(token, lambda _, data: (frames.append(data), ok(data))[1], True, True),
        state=state,
        randbelow=lambda _: pytest.fail("a handoff must not reseed"),
    )
    return owner, frames, token


@pytest.mark.parametrize("effect", ["complete", "node", "text", "debug"])
def test_m1_gap_waits_for_every_retained_public_effect(effect):
    h = Harness(limits=ms.ConfigLimits(pump_actions=1))
    delivered = []
    h.b._on_event = lambda kind, data: delivered.append((kind, bs(h)["publication_admitted"]))
    h.b._on_text = lambda data: delivered.append(("debug", bs(h)["publication_admitted"]))
    ready(h)
    if effect != "complete":
        until(h, lambda: cs(h)["queued_events"] == 0)
        h.feed({"node": node(200), "text": text(), "debug": b"debug\n"}[effect])
        until(h, lambda: cs(h)["queued_events"] > 0)
    assert cs(h)["queued_events"] > 0
    before = bs(h), len(h.writes)
    assert not h.owner.mark_observation_gap()
    assert (bs(h), len(h.writes)) == before
    until(h, lambda: cs(h)["queued_events"] == 0)
    assert h.owner.mark_observation_gap()
    h.begin()
    for _ in range(6):
        h.owner.tick()
    assert not [item for item in delivered if item[0] != "mesh_config_status" and not item[1]]


def record(stage="ready"):
    h = Harness()
    if stage == "untouched":
        pass
    elif stage in {"A", "A-drained"}:
        request = h.begin()
        h.owner.cancel_bootstrap(request)
        if stage == "A-drained":
            h.feed(complete(request.request_id))
    elif stage in {"B", "B-drained"}:
        request = h.to_b()
        h.owner.cancel_bootstrap(request)
        if stage == "B-drained":
            h.feed(complete(h.ids[-1]))
    else:
        h.ready()
        if stage in {"C", "C-drained", "C-gap"}:
            request = h.owner.request_config()
            h.b.cancel_config(request.attempt_id)
            if stage != "C":
                h.feed(complete(request.request_id))
        if stage in {"gap", "C-gap", "later-A"}:
            assert h.owner.mark_observation_gap()
        if stage == "later-A":
            request = h.begin()
            h.owner.cancel_bootstrap(request)
    h.owner.retire()
    return h, h.owner.export_state()


@pytest.mark.parametrize(
    "stage",
    [
        "untouched",
        "A",
        "A-drained",
        "B",
        "B-drained",
        "ready",
        "gap",
        "C",
        "C-drained",
        "C-gap",
        "later-A",
    ],
)
def test_m2_natural_exports_keep_finite_valid_admission(stage):
    h, state = record(stage)
    owner, frames, _ = successor(state)
    view = owner.snapshot()
    assert not frames and not view["config_complete"] and view["nodes"] == []
    assert view["config_status"]["inventory_confirmed"] is False
    assert view["bootstrap_status"]["wire_busy"] is (state.wire.outstanding_id is not None)
    assert view["bootstrap_status"]["synchronized"] is state.synchronized
    assert view["bootstrap_status"]["phase"] != "unverified_idle" or state.operation == 0
    if state.last_config is not None:
        assert (
            view["config_status"]["last_config_outcome"]["session_id"]
            == state.last_config.session_id
        )
        assert view["session_id"] != state.last_config.session_id


@pytest.mark.parametrize(
    "change",
    [
        {"operation": 0},
        {"phase": "unverified_idle"},
        {"sync_id": 500},
        {"inventory_id": 500},
        {"inventory_id": 123},
        {"inventory_id": None},
        {"disconnect": "not_started"},
        {"reason": "fake-ready-error"},
        {"last_config": None},
        {"phase": []},
        {"disconnect": []},
        {"outstanding_purpose": []},
    ],
)
def test_m2_ready_record_rejects_cross_field_and_issued_history_conflicts(change):
    _, state = record()
    with pytest.raises(ValueError):
        replace(state, **change)


@pytest.mark.parametrize(
    "change",
    [
        {"synchronized": True},
        {"inventory_id": 124},
        {"outstanding_purpose": "inventory"},
        {"phase": "ready"},
    ],
)
def test_m2_failed_a_cannot_be_promoted_to_synchronization(change):
    _, state = record("A")
    with pytest.raises(ValueError):
        replace(state, **change)


def test_m2_b_must_be_next_issued_ordinal_not_just_a_valid_id():
    _, state = record("C-drained")
    # C is allocated and valid, but replacing historical B with C skips an ordinal.
    with pytest.raises(ValueError):
        replace(state, inventory_id=state.last_config.request_id)


def test_m2_allocated_new_b_cannot_restore_an_outcome_from_before_its_a():
    h = Harness()
    h.ready()
    prior = h.b._last_config
    assert h.owner.mark_observation_gap()
    request = h.begin()
    h.feed(complete(request.request_id))
    assert len(h.ids) == 4
    h.owner.cancel_bootstrap(request)
    h.owner.retire()
    state = h.owner.export_state()
    with pytest.raises(ValueError):
        replace(state, last_config=prior)


def test_m2_epoch_wrap_is_ordinal_checked_and_later_refresh_tombstone_remains_valid():
    token, frames = object(), []
    owner = MeshWireOwner(
        OrderedSerialBinding(token, lambda _, data: (frames.append(data), ok(data))[1], True, True),
        randbelow=lambda _: ms._ID_COUNT - 1,
    )
    a = owner.begin_bootstrap()
    owner.feed_bytes(token, complete(a.request_id))
    owner.feed_bytes(token, local() + complete(1))
    assert owner.backend.config_complete
    c = owner.request_config()
    assert c.request_id == 2
    owner.backend.cancel_config(c.attempt_id)
    owner.retire()
    state = owner.export_state()
    assert state.sync_id == 0xFFFFFFFF and state.inventory_id == 1
    assert state.wire.outstanding_id == 2
    restored, sent, _ = successor(state)
    assert not restored.request_config().accepted and sent == []


@pytest.mark.parametrize("field", ["scheme", "profile", "last_config"])
def test_m2_reconstructed_v2_records_require_explicit_fields(field):
    _, state = record()
    broken = copy.copy(state)
    object.__delattr__(broken, field)
    with pytest.raises(ValueError):
        successor(broken)


def test_m2_prior_managed_scheme_is_not_reinterpreted():
    _, state = record()
    with pytest.raises(ValueError):
        replace(state, scheme="meshtastic-managed-admission-v1")


@pytest.mark.parametrize("reason", ["cancelled", "timeout", "write_failed"])
@pytest.mark.parametrize("gap", [False, True])
def test_m3_last_refresh_failure_and_original_identity_survive_transfer(reason, gap):
    h = Harness()
    h.ready()
    if reason == "write_failed":
        h.writer = lambda _: (_ for _ in ()).throw(RuntimeError("write"))
        with pytest.raises(RuntimeError):
            h.owner.request_config()
    else:
        request = h.owner.request_config()
        if reason == "cancelled":
            h.b.cancel_config(request.attempt_id)
        else:
            h.b._clock = lambda: 10**12
            h.owner.tick()
    request_id = cs(h)["request_id"]
    h.feed(complete(request_id))
    before = cs(h)["last_config_outcome"]
    assert before["reason"] == reason and before["request_id"] == request_id
    if gap:
        assert h.owner.mark_observation_gap()
    h.owner.retire()
    owner, frames, _ = successor(h.owner.export_state())
    after = owner.snapshot()["config_status"]
    assert after["reason"] == reason and after["request_id"] == request_id
    assert (
        after["last_config_outcome"] == before and owner.backend.session_id != before["session_id"]
    )
    assert (
        after["inventory_confirmed"] is False and not owner.backend.config_complete and not frames
    )


def test_m3_prior_ordinary_outcome_survives_a_later_sync_a_failure():
    h = Harness()
    h.ready()
    c = h.owner.request_config()
    h.b.cancel_config(c.attempt_id)
    h.feed(complete(c.request_id))
    before = cs(h)["last_config_outcome"]
    assert h.owner.mark_observation_gap()
    a = h.begin()
    h.b._clock = lambda: 10**12
    h.owner.tick()
    h.owner.retire()
    state = h.owner.export_state()
    assert state.last_config.request_id < state.sync_id and state.last_config.reason == "cancelled"
    other, frames, _ = successor(state)
    status = other.snapshot()["config_status"]
    assert status["last_config_outcome"] == before and status["request_id"] == a.request_id
    assert status["reason"] == "timeout" and before["reason"] == "cancelled"
    assert status["phase"] == "draining" and not frames


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": 900},
        {"request_id": 123},
        {"session_id": "bad"},
        {"attempt_id": True},
        {"state": "syncing"},
        {"reason": "x" * 65},
    ],
)
def test_m3_outcome_validation_preserves_bounded_coherent_diagnostics(change):
    _, state = record("C-drained")
    with pytest.raises(ValueError):
        replace(state, last_config=replace(state.last_config, **change))
