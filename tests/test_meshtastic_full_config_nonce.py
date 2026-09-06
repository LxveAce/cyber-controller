"""Full-sync nonce amendment; synthetic framing, no firmware/hardware compatibility claim."""
from __future__ import annotations

from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from src.protocols import meshtastic_proto as mp
from src.protocols import meshtastic_stream as ms
from src.protocols.stream_framer import StreamFramer


def request_id(raw):
    return mp.parse(StreamFramer().feed(raw)[0])[3][0]


def complete(number):
    return StreamFramer.frame(mp.field_varint(7, number))


def full_response(number):
    # A full synthetic inventory is appropriate only after the caller has excluded special modes.
    assert number not in (0, 69420, 69421)
    local = mp.field_bytes(3, mp.field_varint(1, 100))
    node = mp.field_bytes(4, mp.field_varint(1, 100))
    channel = mp.field_bytes(10, mp.field_varint(3, 1))
    config = mp.field_bytes(5, mp.field_bytes(6, mp.field_varint(7, 1)))
    return b''.join(StreamFramer.frame(payload) for payload in (local, node, channel, config)) + complete(number)


def sync(backend, writes):
    attempt = backend.start()
    number = request_id(writes[-1])
    assert attempt.accepted and attempt.request_id == number
    backend.feed_bytes(full_response(number))
    assert backend.config_complete and list(backend.nodes) == [100]
    assert backend.my_node_num == 100 and list(backend.channels) == [0] and backend.lora_config.region == 1
    return number


@pytest.mark.parametrize('seed', [69420, 69421])
def test_full_config_rejects_both_explicit_special_mode_seeds(seed):
    writes = []
    with pytest.raises(ValueError):
        ms.MeshtasticBackend(writes.append, config_id=seed)
    assert writes == []


def test_full_config_allocator_crosses_both_reserved_values_without_special_mode_request():
    writes = []
    backend = ms.MeshtasticBackend(writes.append, config_id=69418)
    assert [sync(backend, writes) for _ in range(4)] == [69418, 69419, 69422, 69423]


def test_full_config_wraps_to_one_without_reusing_an_issued_id():
    writes = []
    backend = ms.MeshtasticBackend(writes.append, config_id=0xFFFFFFFF)
    values = [sync(backend, writes) for _ in range(3)]
    assert values == [0xFFFFFFFF, 1, 2] and len(set(values)) == 3


def test_full_config_current_scheme_final_id_then_exhaustion_refuses_wrap_reuse():
    # Inject only allocator progress at the last real rank; no billions-of-iterations test.
    state = ms.ConfigWireState('a' * 32, 1, issued=0xFFFFFFFC, scheme=ms.FULL_CONFIG_WIRE_SCHEME)
    writes = []
    backend = ms.MeshtasticBackend(writes.append, wire_state=state)
    assert sync(backend, writes) == 0xFFFFFFFF
    backend.retire()
    exhausted = backend.export_wire_state()
    assert exhausted.issued == 0xFFFFFFFD and exhausted.outstanding_id is None
    current = ms.MeshtasticBackend(writes.append, wire_state=exhausted)
    assert current.start().reason == 'id_exhausted' and len(writes) == 1


def test_full_config_current_scheme_transfer_without_drain_preserves_rank_progress():
    writes = []
    previous = ms.MeshtasticBackend(writes.append, config_id=69419)
    assert sync(previous, writes) == 69419
    previous.retire()
    state = previous.export_wire_state()
    assert state.scheme == ms.FULL_CONFIG_WIRE_SCHEME and state.issued == 1
    current = ms.MeshtasticBackend(writes.append, wire_state=state)
    assert current.session_id != previous.session_id and sync(current, writes) == 69422
    current.retire()
    exported = current.export_wire_state()
    assert exported.epoch == state.epoch and exported.seed == 69419 and exported.issued == 2
    assert exported.scheme == state.scheme and exported.outstanding_id is None


def test_full_config_current_scheme_transfer_drains_old_id_before_new_full_request():
    writes = []
    previous = ms.MeshtasticBackend(writes.append, config_id=69419)
    old = previous.start()
    previous.retire()
    state = previous.export_wire_state()
    assert state.outstanding_id == old.request_id == 69419
    current = ms.MeshtasticBackend(writes.append, wire_state=state)
    assert current.start().reason == 'busy_draining' and len(writes) == 1
    current.feed_bytes(complete(69421))
    assert not current.start().accepted and len(writes) == 1
    current.feed_bytes(complete(old.request_id))
    assert not current.config_complete
    assert sync(current, writes) == 69422 and len(writes) == 2


@pytest.mark.parametrize('seed', [69420, 69421])
def test_full_config_transfer_rejects_reserved_seed_even_with_current_scheme(seed):
    with pytest.raises(ValueError):
        ms.ConfigWireState('a' * 32, seed, scheme=ms.FULL_CONFIG_WIRE_SCHEME)


@pytest.mark.parametrize('outstanding', [69420, 69421, 69423])
def test_full_config_transfer_outstanding_id_must_match_current_rank(outstanding):
    with pytest.raises(ValueError):
        ms.ConfigWireState('a' * 32, 69419, issued=2, outstanding_id=outstanding,
                           scheme=ms.FULL_CONFIG_WIRE_SCHEME)
    valid = ms.ConfigWireState('a' * 32, 69419, issued=2, outstanding_id=69422,
                               scheme=ms.FULL_CONFIG_WIRE_SCHEME)
    assert valid.outstanding_id == 69422


@pytest.mark.parametrize('issued', [0xFFFFFFFE, -1, True])
def test_full_config_transfer_rejects_count_outside_current_scheme(issued):
    with pytest.raises(ValueError):
        ms.ConfigWireState('a' * 32, 1, issued=issued, scheme=ms.FULL_CONFIG_WIRE_SCHEME)


def test_full_config_legacy_record_requires_explicit_scheme_not_an_implicit_new_default():
    # These exact fields were sufficient in925; issued2 crossed69420 under the old scheme.
    legacy = {'epoch': 'a' * 32, 'seed': 69419, 'issued': 2,
              'outstanding_id': 69421, 'resync_required': False}
    with pytest.raises(TypeError):
        ms.ConfigWireState(**legacy)
    with pytest.raises(ValueError):
        ms.MeshtasticBackend(lambda _: pytest.fail('legacy record wrote'), wire_state=SimpleNamespace(**legacy))


@pytest.mark.parametrize('scheme', [None, 'legacy-excluding-0-69420', 'future-unknown-scheme'])
def test_full_config_transfer_rejects_incompatible_explicit_scheme(scheme):
    with pytest.raises(ValueError):
        ms.ConfigWireState('a' * 32, 1, scheme=scheme)


@pytest.mark.parametrize('missing', [False, True])
def test_full_config_backend_revalidates_scheme_when_admitting_reconstructed_object(missing):
    backend = ms.MeshtasticBackend(lambda _: None, config_id=69419)
    backend.retire()
    state = backend.export_wire_state()
    # Simulate an incompatible private object reconstructed without dataclass __post_init__.
    # Do not add persistence or a legacy-count conversion to the production contract.
    if missing:
        object.__delattr__(state, 'scheme')
    else:
        object.__setattr__(state, 'scheme', 'legacy-excluding-0-69420')
    with pytest.raises(ValueError):
        ms.MeshtasticBackend(lambda _: pytest.fail('incompatible object wrote'), wire_state=state)


def test_full_config_current_record_reconstruction_and_export_keep_scheme_explicit():
    backend = ms.MeshtasticBackend(lambda _: None)
    backend.retire()
    state = backend.export_wire_state()
    rebuilt = ms.ConfigWireState(**asdict(state))
    assert rebuilt == state and replace(rebuilt, issued=1).scheme == state.scheme


def test_generic_nonce_encoding_remains_separate_from_full_config_allocation():
    # Existing generic encoder behavior is preserved, not claimed to establish a full dump.
    assert mp.parse(mp.encode_want_config(69421))[3][0] == 69421
    assert mp.parse(mp.encode_want_config(69420))[3][0] == 69421  # historical bump behavior
    assert mp.NODES_ONLY_WANT_CONFIG_ID == 69421
