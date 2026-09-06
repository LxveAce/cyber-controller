"""MDBF-1/2: bounded admitted identities and one coherent result for each retained request."""

from dataclasses import replace

import pytest
from test_meshtastic_bootstrap import Harness, complete, local, ok
from test_meshtastic_managed_handoff import record, successor

from src.core.meshtastic_owner import MeshWireOwner, OrderedSerialBinding
from src.protocols import meshtastic_stream as ms


def seeded_ready(rank):
    token = object()
    owner = MeshWireOwner(
        OrderedSerialBinding(token, lambda _, data: ok(data), True, True),
        randbelow=lambda _: rank,
    )
    a = owner.begin_bootstrap()
    owner.feed_bytes(token, complete(a.request_id))
    b = owner.snapshot()["bootstrap_status"]["inventory_id"]
    owner.feed_bytes(token, local() + complete(b))
    assert owner.backend.config_complete
    owner.retire()
    return owner.export_state()


@pytest.mark.parametrize("rank", [0, 69418, ms._ID_COUNT - 1])
@pytest.mark.parametrize("field", ["operation", "outcome_attempt"])
@pytest.mark.parametrize("large", [False, True])
def test_mdbf1_counters_cannot_exceed_their_request_progress(rank, field, large):
    state = seeded_ready(rank)
    # A is ordinal 0 and B ordinal 1 even around special IDs and uint32 wrap.
    number = ms._REVISION_LIMIT if large else (2 if field == "operation" else 3)
    with pytest.raises(ValueError):
        if field == "operation":
            replace(state, operation=number)
        else:
            replace(state, last_config=replace(state.last_config, attempt_id=number))


@pytest.mark.parametrize("rank", [0, 69418, ms._ID_COUNT - 1])
def test_mdbf1_honest_boundary_ids_allow_cancellation_drain_and_new_success(rank):
    state = seeded_ready(rank)
    owner, frames, token = successor(state)
    request = owner.request_config()
    assert request.accepted and len(frames) == 1
    assert owner.backend.cancel_config(request.attempt_id)
    assert owner.snapshot()["config_status"]["reason"] == "cancelled"
    assert not owner.request_config().accepted
    owner.feed_bytes(token, complete(request.request_id))
    following = owner.request_config()
    assert following.accepted and following.attempt_id == request.attempt_id + 1
    owner.feed_bytes(token, local() + complete(following.request_id))
    assert owner.backend.config_complete
    outcome = owner.snapshot()["config_status"]["last_config_outcome"]
    assert outcome["request_id"] == following.request_id and outcome["state"] == "ready"


def test_mdbf1_smaller_historical_counters_remain_compatible():
    h = Harness()
    h.ready()
    assert h.owner.mark_observation_gap()
    a = h.begin()
    h.feed(complete(a.request_id))
    h.feed(local() + complete(h.ids[-1]))
    h.owner.retire()
    state = h.owner.export_state()
    lower = replace(state, operation=1, last_config=replace(state.last_config, attempt_id=2))
    owner, frames, _ = successor(lower)
    request = owner.request_config()
    assert request.accepted and len(frames) == 1
    assert owner.backend.cancel_config(request.attempt_id)


def test_mdbf2_failed_same_b_must_preserve_its_exact_reason():
    _, state = record("B-drained")
    assert state.reason == state.last_config.reason == "cancelled"
    with pytest.raises(ValueError):
        replace(state, last_config=replace(state.last_config, reason="timeout"))


def test_mdbf2_tombstone_cannot_substitute_an_older_failed_inventory_outcome():
    h = Harness()
    a = h.to_b()
    h.owner.cancel_bootstrap(a)
    prior = h.b._last_config
    h.feed(complete(h.ids[-1]))
    c = h.owner.request_config()
    assert c.accepted
    h.b.cancel_config(c.attempt_id)
    h.owner.retire()
    state = h.owner.export_state()
    with pytest.raises(ValueError):
        replace(state, last_config=prior)


@pytest.mark.parametrize("c_success", [False, True])
def test_mdbf2_later_c_outcome_is_independent_of_failed_historical_b(c_success):
    h = Harness()
    a = h.to_b()
    h.owner.cancel_bootstrap(a)
    h.feed(complete(h.ids[-1]))
    c = h.owner.request_config()
    if c_success:
        h.feed(local() + complete(c.request_id))
    else:
        h.b._clock = lambda: 10**12
        h.owner.tick()
        h.feed(complete(c.request_id))
    h.owner.retire()
    state = h.owner.export_state()
    assert state.phase == "failed" and state.reason == "cancelled"
    assert state.last_config.request_id == c.request_id
    assert state.last_config.reason == (None if c_success else "timeout")
    owner, frames, _ = successor(state)
    assert not frames and not owner.backend.config_complete
    assert owner.snapshot()["config_status"]["last_config_outcome"]["request_id"] == c.request_id
