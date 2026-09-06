"""Result/correlation invariants; no transport, device or server is started."""

import json
from dataclasses import FrozenInstanceError

import pytest

from src.core.command_dispatch import (
    MAX_STEP_INDEX,
    DispatchErrorCode,
    DispatchOutcome,
    DispatchResult,
    RequestCorrelation,
)

CASES = [
    (DispatchOutcome.INVALID_INPUT, DispatchErrorCode.INVALID_REQUEST, 400, False),
    (DispatchOutcome.UNAUTHENTICATED, DispatchErrorCode.AUTHENTICATION_REQUIRED, 401, False),
    (DispatchOutcome.UNAUTHORIZED, DispatchErrorCode.PERMISSION_DENIED, 403, False),
    (DispatchOutcome.STALE_BINDING, DispatchErrorCode.BINDING_CHANGED, 409, False),
    (DispatchOutcome.PORT_BUSY, DispatchErrorCode.PORT_RESERVED, 409, False),
    (DispatchOutcome.UNSUPPORTED_TRANSPORT, DispatchErrorCode.TEXT_NOT_SUPPORTED, 422, False),
    (DispatchOutcome.UNAVAILABLE, DispatchErrorCode.CONNECTION_UNAVAILABLE, 503, False),
    (DispatchOutcome.UNAVAILABLE, DispatchErrorCode.SERVICE_UNAVAILABLE, 503, False),
    (DispatchOutcome.DEFINITELY_NOT_WRITTEN, DispatchErrorCode.ZERO_WRITE, 502, False),
    (DispatchOutcome.DELIVERY_UNCERTAIN, DispatchErrorCode.SHORT_WRITE, 502, True),
    (DispatchOutcome.DELIVERY_UNCERTAIN, DispatchErrorCode.INVALID_WRITE_COUNT, 502, True),
    (DispatchOutcome.DELIVERY_UNCERTAIN, DispatchErrorCode.WRITE_ERROR, 502, True),
    (DispatchOutcome.DELIVERY_UNCERTAIN, DispatchErrorCode.FLUSH_ERROR, 502, True),
    (DispatchOutcome.DELIVERY_UNCERTAIN, DispatchErrorCode.UNKNOWN_DELIVERY, 502, True),
    (DispatchOutcome.HOST_WRITE_COMPLETE, None, 200, True),
]


@pytest.mark.parametrize("outcome,code,status,may_write", CASES)
def test_complete_outcome_matrix(outcome, code, status, may_write):
    correlation = RequestCorrelation.create(run_id="b" * 32, step_index=0)
    result = DispatchResult(correlation, outcome, code)
    assert result.http_status == status
    assert result.retryable is False
    assert result.may_have_written is may_write
    assert result.host_write_complete is (outcome is DispatchOutcome.HOST_WRITE_COMPLETE)
    payload = result.to_dict()
    assert json.loads(json.dumps(payload, allow_nan=False)) == payload
    assert payload == {
        "schema_version": 1, "request_id": correlation.request_id,
        "run_id": "b" * 32, "step_index": 0, "outcome": outcome.value,
        "error_code": code.value if code is not None else None,
        "retryable": False, "may_have_written": may_write,
    }
    assert all(type(value) in (str, int, bool, type(None)) for value in payload.values())


def test_fixture_covers_every_outcome_and_error_code():
    assert {case[0] for case in CASES} == set(DispatchOutcome)
    assert {case[1] for case in CASES if case[1] is not None} == set(DispatchErrorCode)


@pytest.mark.parametrize("outcome", list(DispatchOutcome))
def test_result_cannot_be_mistaken_for_a_success_boolean(outcome):
    code = next(case[1] for case in CASES if case[0] is outcome)
    result = DispatchResult(RequestCorrelation.create(), outcome, code)
    with pytest.raises(TypeError, match="Inspect the dispatch outcome"):
        bool(result)


@pytest.mark.parametrize("outcome,code", [
    (DispatchOutcome.HOST_WRITE_COMPLETE, DispatchErrorCode.WRITE_ERROR),
    (DispatchOutcome.DELIVERY_UNCERTAIN, DispatchErrorCode.ZERO_WRITE),
    (DispatchOutcome.DEFINITELY_NOT_WRITTEN, DispatchErrorCode.SHORT_WRITE),
    (DispatchOutcome.UNAVAILABLE, DispatchErrorCode.FLUSH_ERROR),
    (DispatchOutcome.UNAUTHORIZED, None),
])
def test_contradictory_evidence_is_rejected(outcome, code):
    with pytest.raises(ValueError, match="does not match"):
        DispatchResult(RequestCorrelation.create(), outcome, code)


class TextSubtype(str):
    pass


class IntSubtype(int):
    pass


@pytest.mark.parametrize("bad", [None, True, "", "A" * 32, "a" * 31, "a" * 33,
                                 "g" * 32, "a" * 32 + "\n", TextSubtype("a" * 32), []])
def test_correlation_rejects_invalid_or_mutable_ids(bad):
    with pytest.raises(ValueError):
        RequestCorrelation(bad)
    with pytest.raises(ValueError):
        RequestCorrelation("a" * 32, bad, 0)


@pytest.mark.parametrize("step", [None, True, False, -1, 0.0, MAX_STEP_INDEX + 1,
                                  IntSubtype(0), "0", []])
def test_run_step_requires_exact_bounded_integer(step):
    with pytest.raises(ValueError):
        RequestCorrelation.create(run_id="b" * 32, step_index=step)


@pytest.mark.parametrize("step", [0, MAX_STEP_INDEX])
def test_run_step_boundary_serializes_exactly(step):
    result = DispatchResult(RequestCorrelation.create(run_id="b" * 32, step_index=step),
                            DispatchOutcome.HOST_WRITE_COMPLETE)
    assert json.loads(json.dumps(result.to_dict()))["step_index"] == step


def test_standalone_request_has_no_playback_context_and_ids_are_generated():
    first = RequestCorrelation.create()
    second = RequestCorrelation.create()
    assert first.request_id != second.request_id
    assert first.run_id is None and first.step_index is None
    with pytest.raises(ValueError, match="both"):
        RequestCorrelation.create(step_index=0)


def test_payload_is_detached_and_contains_no_arbitrary_details():
    result = DispatchResult(RequestCorrelation.create(), DispatchOutcome.HOST_WRITE_COMPLETE)
    first = result.to_dict()
    first["outcome"] = "fabricated"
    first["command"] = "synthetic-private-command"
    second = result.to_dict()
    assert second["outcome"] == "host_write_complete"
    assert "command" not in second
    assert "synthetic-private-command" not in repr(result)
    assert not hasattr(result, "__dict__")
    with pytest.raises(FrozenInstanceError):
        result.outcome = DispatchOutcome.UNAVAILABLE
    with pytest.raises(FrozenInstanceError):
        result.correlation.request_id = "c" * 32


@pytest.mark.parametrize("field,value", [
    ("outcome", "delivery_uncertain"), ("outcome", TextSubtype("delivery_uncertain")),
    ("error_code", "short_write"), ("error_code", TextSubtype("short_write")),
    ("correlation", {"request_id": "a" * 32}),
])
def test_result_requires_exact_validated_types(field, value):
    values = {"correlation": RequestCorrelation.create(),
              "outcome": DispatchOutcome.DELIVERY_UNCERTAIN,
              "error_code": DispatchErrorCode.SHORT_WRITE}
    values[field] = value
    with pytest.raises(ValueError):
        DispatchResult(**values)


def test_rejected_values_never_enter_exception_messages():
    secret = "synthetic-password-or-command"
    operations = [
        lambda: RequestCorrelation(secret),
        lambda: RequestCorrelation("a" * 32, secret, 0),
        lambda: DispatchResult(RequestCorrelation.create(), secret),
        lambda: DispatchResult(
            RequestCorrelation.create(), DispatchOutcome.DELIVERY_UNCERTAIN, secret,
        ),
    ]
    for operation in operations:
        with pytest.raises(ValueError) as error:
            operation()
        assert secret not in str(error.value) and secret not in repr(error.value)
