"""Strict, hardware-inert contract tests for the deterministic text replayer.

These tests intentionally exercise a parser-level line simulation, not serial/HIL
parity.  The fixture may request only the read-only ``status`` command and every
device-originated fact is visibly synthetic.  Nothing in this file may enumerate,
open, or write a host serial port.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from src.core.serial_handler import ConnectionState
from src.core.transports import (
    ReplayConnection,
    ReplayMismatchError,
    ReplayStateError,
    TranscriptValidationError,
    TransportTranscript,
    load_transcript,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "transports"
FIXTURE = FIXTURE_ROOT / "lxveos-passive-v1.json"


def _fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def _fixture_dict() -> dict:
    return json.loads(_fixture_bytes())


def _encoded(payload: dict, *, pretty: bool = False) -> bytes:
    if pretty:
        return json.dumps(payload, ensure_ascii=False, indent=4).encode("utf-8")
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _transcript(payload: dict) -> TransportTranscript:
    return TransportTranscript.from_bytes(_encoded(payload))


def _connection() -> ReplayConnection:
    transcript = load_transcript(FIXTURE, fixture_root=FIXTURE_ROOT)
    return ReplayConnection(transcript)


def _all_mapping_keys(value) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        keys.update(str(key) for key in value)
        for child in value.values():
            keys.update(_all_mapping_keys(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            keys.update(_all_mapping_keys(child))
    return keys


def _all_string_values(value) -> set[str]:
    strings: set[str] = set()
    if isinstance(value, str):
        strings.add(value)
    elif isinstance(value, dict):
        for child in value.values():
            strings.update(_all_string_values(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            strings.update(_all_string_values(child))
    return strings


def test_fixture_is_explicitly_synthetic_and_passive() -> None:
    payload = _fixture_dict()
    assert payload["schema"] == "CyberControllerTransportTranscript@1"
    assert payload["device"]["simulated"] is True
    assert payload["device"]["port"].startswith("sim://")
    assert payload["connection"] == {
        "mode": "text",
        "baud": 115200,
        "encoding": "utf-8",
        "line_ending": "\n",
    }
    assert [
        event["text"] for event in payload["events"] if event["kind"] == "expect_text_write"
    ] == ["status"]

    received = [event["text"] for event in payload["events"] if event["kind"] == "rx_line"]
    assert received
    assert all("synthetic=1" in line for line in received)
    status = next(line for line in received if " status " in line)
    assert "arm=safe" in status and "tx=0" in status
    assert "ops=" not in status  # no hand-maintained operation tally can drift
    assert "bssid=02:00:00:00:00:01" in received[1]  # locally administered


def test_loader_binds_raw_source_and_semantic_transcript_hashes() -> None:
    raw = _fixture_bytes()
    transcript = load_transcript(FIXTURE, fixture_root=FIXTURE_ROOT)
    assert isinstance(transcript, TransportTranscript)
    assert transcript.source_sha256 == hashlib.sha256(raw).hexdigest()
    assert len(transcript.transcript_sha256) == 64

    # Whitespace is byte provenance, not semantic identity.
    repacked = TransportTranscript.from_bytes(_encoded(_fixture_dict(), pretty=True))
    assert repacked.source_sha256 != transcript.source_sha256
    assert repacked.transcript_sha256 == transcript.transcript_sha256


def test_validated_transcript_cannot_be_directly_constructed_or_replaced() -> None:
    transcript = load_transcript(FIXTURE, fixture_root=FIXTURE_ROOT)
    with pytest.raises(TypeError):
        TransportTranscript()
    with pytest.raises(TypeError):
        replace(transcript, events=())


def test_replay_admission_revalidates_a_forged_in_process_object() -> None:
    transcript = load_transcript(FIXTURE, fixture_root=FIXTURE_ROOT)
    forged_device = object.__new__(TransportTranscript)
    forged_source = object.__new__(TransportTranscript)
    forged_equivalent = object.__new__(TransportTranscript)
    for forged in (forged_device, forged_source, forged_equivalent):
        for name in transcript.__dataclass_fields__:
            object.__setattr__(forged, name, getattr(transcript, name))
    object.__setattr__(
        forged_device,
        "device",
        replace(transcript.device, port="COM3", simulated=False),
    )
    with pytest.raises(TranscriptValidationError):
        ReplayConnection(forged_device)

    object.__setattr__(forged_source, "source_sha256", "0" * 64)
    with pytest.raises(TranscriptValidationError):
        ReplayConnection(forged_source)

    admitted = ReplayConnection(forged_equivalent)
    assert admitted._transcript is not forged_equivalent


def test_loader_rejects_paths_outside_the_explicit_fixture_root(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(_fixture_bytes())

    with pytest.raises(TranscriptValidationError):
        load_transcript(outside, fixture_root=trusted)


def test_loader_rejects_non_files_and_url_like_paths(tmp_path: Path) -> None:
    with pytest.raises(TranscriptValidationError):
        load_transcript(tmp_path, fixture_root=tmp_path)
    with pytest.raises(TranscriptValidationError):
        load_transcript("https://invalid.example/fixture.json", fixture_root=tmp_path)


@pytest.mark.parametrize(
    "path",
    ["../escape.json", "fixture*.json", "~\\fixture.json", "$env:TEMP\\fixture.json"],
)
def test_loader_rejects_traversal_glob_and_expansion_syntax(tmp_path: Path, path: str) -> None:
    with pytest.raises(TranscriptValidationError):
        load_transcript(path, fixture_root=tmp_path)


@pytest.mark.parametrize(
    "path",
    ["COM1", "COM1.json", "NUL.json", "nested/AUX.fixture", "LPT9.ignored"],
)
def test_loader_rejects_windows_reserved_device_components(tmp_path: Path, path: str) -> None:
    with pytest.raises(TranscriptValidationError):
        load_transcript(path, fixture_root=tmp_path)


def test_loader_bounds_path_inputs_before_filesystem_resolution(tmp_path: Path) -> None:
    oversized = "a" * 4_097
    with pytest.raises(TranscriptValidationError):
        load_transcript(oversized, fixture_root=tmp_path)
    with pytest.raises(TranscriptValidationError):
        load_transcript("fixture.json", fixture_root=oversized)
    with pytest.raises(TranscriptValidationError):
        load_transcript("\ud800", fixture_root=tmp_path)
    with pytest.raises(TranscriptValidationError):
        load_transcript("bad\x00name.json", fixture_root=tmp_path)
    with pytest.raises(TranscriptValidationError):
        load_transcript("fixture.json", fixture_root="bad\x00root")


def test_loader_rejects_a_symlink_even_when_it_resolves_inside_root(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_bytes(_fixture_bytes())
    alias = tmp_path / "alias.json"
    try:
        alias.symlink_to(target)
    except OSError:
        pytest.skip("host policy does not permit symlink creation")
    with pytest.raises(TranscriptValidationError):
        load_transcript(alias, fixture_root=tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-containment regression")
@pytest.mark.parametrize("redirect_outside_root", [False, True])
def test_windows_open_handle_must_match_validated_path(
    tmp_path: Path, monkeypatch, redirect_outside_root: bool
) -> None:
    import src.core.transports.replay as replay_module

    trusted = tmp_path / "trusted"
    trusted.mkdir()
    inside = trusted / "fixture.json"
    inside.write_bytes(_fixture_bytes())
    alias_parent = tmp_path if redirect_outside_root else trusted
    redirected_alias = alias_parent / "redirected-hardlink.json"
    os.link(inside, redirected_alias)
    expected_open = inside.resolve()
    real_open = os.open

    def redirected_open(path, flags, *args, **kwargs):
        if Path(path) == expected_open:
            return real_open(redirected_alias, flags, *args, **kwargs)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(replay_module.os, "open", redirected_open)
    with pytest.raises(TranscriptValidationError):
        load_transcript(inside, fixture_root=trusted)


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "mkfifo"),
    reason="POSIX FIFO/open-flags regression",
)
def test_posix_fifo_is_rejected_without_a_blocking_leaf_open(tmp_path: Path, monkeypatch) -> None:
    import src.core.transports.replay as replay_module

    fifo = tmp_path / "fixture.json"
    os.mkfifo(fifo)
    real_open = os.open

    def guarded_open(path, flags, *args, **kwargs):
        if path == fifo.name and kwargs.get("dir_fd") is not None:
            assert flags & os.O_NONBLOCK
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(replay_module.os, "open", guarded_open)
    descriptor = replay_module._open_beneath_root_posix(tmp_path, Path(fifo.name))
    os.close(descriptor)
    with pytest.raises(TranscriptValidationError):
        load_transcript(fifo, fixture_root=tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-walk regression")
def test_posix_leaf_open_is_nonblocking_before_post_open_type_check(
    tmp_path: Path, monkeypatch
) -> None:
    import src.core.transports.replay as replay_module

    fixture = tmp_path / "fixture.json"
    fixture.write_bytes(_fixture_bytes())
    real_open = os.open
    observed_leaf = False

    def guarded_open(path, flags, *args, **kwargs):
        nonlocal observed_leaf
        if path == fixture.name and kwargs.get("dir_fd") is not None:
            observed_leaf = True
            assert flags & os.O_NONBLOCK
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(replay_module.os, "open", guarded_open)
    assert load_transcript(fixture, fixture_root=tmp_path).scenario_id == "lxveos-passive-ap"
    assert observed_leaf is True


@pytest.mark.parametrize("field", ["seq", "at_ms"])
def test_validator_rejects_bool_where_an_integer_is_required(field: str) -> None:
    payload = _fixture_dict()
    payload["events"][0][field] = True
    with pytest.raises(TranscriptValidationError):
        _transcript(payload)


def test_validator_rejects_duplicate_and_unknown_keys() -> None:
    raw = _fixture_bytes()
    duplicate = raw.replace(
        b'"schema": "CyberControllerTransportTranscript@1",',
        b'"schema": "CyberControllerTransportTranscript@1",\n'
        b'  "schema": "CyberControllerTransportTranscript@1",',
        1,
    )
    with pytest.raises(TranscriptValidationError):
        TransportTranscript.from_bytes(duplicate)

    payload = _fixture_dict()
    payload["unexpected"] = "typo-must-not-be-ignored"
    with pytest.raises(TranscriptValidationError):
        _transcript(payload)

    payload = _fixture_dict()
    payload["events"][0]["unexpected"] = True
    with pytest.raises(TranscriptValidationError):
        _transcript(payload)


def test_validator_rejects_nested_duplicates_without_echoing_hostile_key() -> None:
    raw = _fixture_bytes().replace(
        b'"simulated": true',
        b'"simulated": true, "attacker-controlled-secret": 1, "attacker-controlled-secret": 2',
        1,
    )
    with pytest.raises(TranscriptValidationError) as caught:
        TransportTranscript.from_bytes(raw)
    assert "attacker-controlled-secret" not in str(caught.value)


@pytest.mark.parametrize(
    "replacement",
    [b"1.0", b"NaN", b"Infinity", b"123456789012345678901"],
    ids=["float", "nan", "infinity", "huge-integer"],
)
def test_validator_rejects_noninteger_numeric_spellings(replacement: bytes) -> None:
    raw = _fixture_bytes().replace(b'"at_ms": 0', b'"at_ms": ' + replacement, 1)
    with pytest.raises(TranscriptValidationError):
        TransportTranscript.from_bytes(raw)


def test_validator_rejects_json_escaped_lone_surrogate() -> None:
    payload = _fixture_dict()
    payload["device"]["name"] = "\ud800"
    raw = json.dumps(payload, ensure_ascii=True).encode("ascii")
    with pytest.raises(TranscriptValidationError):
        TransportTranscript.from_bytes(raw)


@pytest.mark.parametrize(
    "raw",
    [
        b"\xef\xbb\xbf" + _fixture_bytes(),
        b'{"schema":\xff}',
    ],
    ids=["utf8-bom", "invalid-utf8"],
)
def test_validator_rejects_noncanonical_text_encodings(raw: bytes) -> None:
    with pytest.raises(TranscriptValidationError):
        TransportTranscript.from_bytes(raw)


def test_validator_rejects_oversized_source_before_json_expansion() -> None:
    raw = _fixture_bytes()
    oversized = raw + b" " * (1_048_577 - len(raw))
    assert len(oversized) == 1_048_577
    with pytest.raises(TranscriptValidationError):
        TransportTranscript.from_bytes(oversized)


@pytest.mark.parametrize(
    ("event_index", "size"),
    [(1, 4_097), (2, 16_385)],
    ids=["expected-write", "received-line"],
)
def test_validator_rejects_oversized_event_text(event_index: int, size: int) -> None:
    payload = _fixture_dict()
    payload["events"][event_index]["text"] = "x" * size
    with pytest.raises(TranscriptValidationError):
        _transcript(payload)


@pytest.mark.parametrize("bad_text", ["sta\ntus", "sta\rtus", "sta\x00tus", "sta\x7ftus"])
def test_validator_rejects_control_characters_in_fixture_text(bad_text: str) -> None:
    payload = _fixture_dict()
    payload["events"][1]["text"] = bad_text
    with pytest.raises(TranscriptValidationError):
        _transcript(payload)


def test_validator_rejects_bad_sequence_time_and_lifecycle() -> None:
    payload = _fixture_dict()
    payload["events"][2]["seq"] = 99
    with pytest.raises(TranscriptValidationError):
        _transcript(payload)

    payload = _fixture_dict()
    payload["events"] = [
        {"seq": 1, "at_ms": 0, "kind": "connect"},
        {"seq": 2, "at_ms": 1, "kind": "disconnect", "reason": "scripted_end"},
        {"seq": 3, "at_ms": 2, "kind": "connect"},
        {"seq": 4, "at_ms": 3, "kind": "disconnect", "reason": "scripted_end"},
    ]
    with pytest.raises(TranscriptValidationError):
        _transcript(payload)

    payload = _fixture_dict()
    payload["events"][3]["at_ms"] = 0
    with pytest.raises(TranscriptValidationError):
        _transcript(payload)

    payload = _fixture_dict()
    payload["events"][-1] = {
        "seq": 5,
        "at_ms": 3,
        "kind": "rx_line",
        "text": "LXVEOS/1 bridge state=off synthetic=1",
    }
    with pytest.raises(TranscriptValidationError):
        _transcript(payload)


@pytest.mark.parametrize("line_ending", ["", "\n\n", "\x00", "native"])
def test_validator_allows_only_one_declared_serial_terminator(line_ending: str) -> None:
    payload = _fixture_dict()
    payload["connection"]["line_ending"] = line_ending
    with pytest.raises(TranscriptValidationError):
        _transcript(payload)


def test_validator_requires_an_opaque_simulated_port_and_truth_flag() -> None:
    payload = _fixture_dict()
    payload["device"]["port"] = "COM3"
    with pytest.raises(TranscriptValidationError):
        _transcript(payload)

    payload = _fixture_dict()
    payload["device"]["simulated"] = False
    with pytest.raises(TranscriptValidationError):
        _transcript(payload)


def test_validator_keeps_declared_basis_metadata_opaque() -> None:
    payload = _fixture_dict()
    payload["basis"] = {
        "schema": "LxveOSBoardManifest@1",
        "sha256": "a" * 64,
        "entry_id": r"C:\Users\secret\commands.txt",
    }
    with pytest.raises(TranscriptValidationError):
        _transcript(payload)


def test_connect_write_and_timed_receive_follow_explicit_barriers() -> None:
    conn = _connection()
    states: list[ConnectionState] = []
    lines: list[str] = []
    conn.on_state_change(states.append)
    conn.on_line(lines.append)

    assert conn.state is ConnectionState.DISCONNECTED
    assert conn.is_connected is False
    assert conn.cursor == 0
    assert conn.virtual_ms == 0
    assert conn.blocked_on == "connect"
    assert conn.run_status == "not_started"

    conn.connect()
    assert conn.state is ConnectionState.CONNECTED
    assert states == [ConnectionState.CONNECTING, ConnectionState.CONNECTED]
    assert conn.cursor == 1
    assert conn.blocked_on == "write"
    assert conn.run_status == "running"

    conn.connect()  # connected connect is idempotent, as on SerialConnection
    assert states == [ConnectionState.CONNECTING, ConnectionState.CONNECTED]

    # Match SerialConnection._command_payload: trailing CR/LF is stripped once,
    # then exactly the transcript-pinned terminator is appended.
    assert conn.write("status\r\n") is None
    assert conn.cursor == 2
    assert conn.blocked_on == "time"
    assert lines == []

    assert conn.advance_to(2) == 2
    assert lines == [
        "LXVEOS/1 status caps=0x003 arm=safe tx=0 synthetic=1",
        "LXVEOS/1 ap bssid=02:00:00:00:00:01 "
        "ssid=666978747572652d6170 ch=1 rssi=-60 auth=wpa2 synthetic=1",
    ]
    assert conn.state is ConnectionState.CONNECTED
    assert conn.blocked_on == "time"

    assert conn.advance_to(3) == 1
    assert conn.state is ConnectionState.DISCONNECTED
    assert states[-1] is ConnectionState.DISCONNECTED
    assert conn.blocked_on == "done"
    assert conn.run_status == "passed"
    assert conn.complete is True

    conn.disconnect()  # completed disconnect is idempotent
    assert states.count(ConnectionState.DISCONNECTED) == 1


def test_simulation_identity_and_transport_shape_are_read_only() -> None:
    conn = _connection()
    for name, value in {
        "port": "COM3",
        "baud": -1,
        "encoding": "latin-1",
        "simulated": False,
        "parser_level_simulation": False,
        "hardware_evidence": True,
        "raw": True,
        "timeout": 10.0,
    }.items():
        with pytest.raises(AttributeError):
            setattr(conn, name, value)
    assert conn.port.startswith("sim://")
    assert conn.simulated is True
    assert conn.hardware_evidence is False


def test_advance_retains_target_time_but_never_crosses_an_action_barrier() -> None:
    conn = _connection()
    assert conn.advance_to(99) == 0
    assert conn.virtual_ms == 99
    assert conn.cursor == 0
    assert conn.blocked_on == "connect"

    conn.connect()
    assert conn.cursor == 1
    assert conn.blocked_on == "write"


def test_advance_by_owns_drive_before_deriving_its_target() -> None:
    class NoPublicAdvanceToReplay(ReplayConnection):
        def advance_to(self, _target_ms: int) -> int:
            raise AssertionError("advance_by must not derive a target before drive ownership")

    conn = NoPublicAdvanceToReplay(load_transcript(FIXTURE, fixture_root=FIXTURE_ROOT))
    conn.connect()
    conn.write("status")
    assert conn.advance_by(1) == 1
    assert conn.virtual_ms == 1


def test_concurrent_advance_by_fails_closed_instead_of_losing_a_delta() -> None:
    conn = _connection()
    entered = threading.Event()
    release = threading.Event()
    owner_failures: list[BaseException] = []

    def block(_line: str) -> None:
        entered.set()
        assert release.wait(2)

    def owner() -> None:
        try:
            conn.advance_by(2)
        except BaseException as exc:
            owner_failures.append(exc)

    conn.on_line(block)
    conn.connect()
    conn.write("status")
    worker = threading.Thread(target=owner)
    worker.start()
    assert entered.wait(2)
    with pytest.raises(ReplayStateError):
        conn.advance_by(1)
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert owner_failures == []
    assert conn.virtual_ms == 2
    assert conn.run_status == "operator_stopped"
    assert conn.state is ConnectionState.ERROR
    assert conn.snapshot().terminal_reason == "concurrent_or_reentrant_drive"


def test_runtime_command_controls_are_rejected_without_consuming_the_barrier() -> None:
    conn = _connection()
    conn.connect()
    for value in ("sta\ntus", "sta\rtus", "sta\x00tus", "sta\x7ftus"):
        with pytest.raises(ValueError):
            conn.write(value)
        assert conn.state is ConnectionState.CONNECTED
        assert conn.cursor == 1

    conn.line_ending = "\n"  # TextCliDriver stamps the same value; that is allowed.
    with pytest.raises(ReplayStateError):
        conn.line_ending = "\r"
    assert conn.state is ConnectionState.CONNECTED
    assert conn.cursor == 1


def test_runtime_command_bound_applies_before_trailing_terminator_stripping() -> None:
    conn = _connection()
    conn.connect()
    with pytest.raises(ValueError):
        conn.write("status" + "\n" * 4_091)
    assert conn.cursor == 1
    assert conn.run_status == "input_rejected"


def test_rejected_runtime_input_is_payload_free_and_prevents_false_pass() -> None:
    conn = _connection()
    conn.connect()
    with pytest.raises(ValueError):
        conn.write("bad\ncommand")
    conn.write("status")
    conn.advance_to(3)

    assert conn.run_status == "input_rejected"
    assert conn.complete is False
    evidence = conn.snapshot().to_dict()
    assert evidence["input_error_count"] == 1
    assert "bad" not in json.dumps(evidence, sort_keys=True)
    before = conn.snapshot().run_sha256
    conn.cancel()
    conn.disconnect()
    assert conn.snapshot().run_sha256 == before
    with pytest.raises(ReplayStateError):
        conn.connect()


def test_unexpected_write_fails_closed_without_payload_disclosure() -> None:
    conn = _connection()
    errors: list[Exception] = []
    conn.on_error(errors.append)
    conn.connect()

    actual = "wrong-secret-like-command"
    with pytest.raises(ReplayMismatchError) as caught:
        conn.write(actual)

    assert conn.state is ConnectionState.ERROR
    assert conn.cursor == 1  # the expected-write barrier was not consumed
    assert conn.run_status == "mismatch"
    assert conn.complete is False
    assert errors == [caught.value]
    message = str(caught.value)
    assert actual not in message
    assert "status" not in message

    evidence = conn.snapshot().to_dict()
    evidence_text = json.dumps(evidence, sort_keys=True)
    assert actual not in evidence_text
    assert "wire_sha256" not in _all_mapping_keys(evidence)
    assert "payload_sha256" not in _all_mapping_keys(evidence)


def test_mismatch_evidence_commits_only_safe_lengths_and_reason() -> None:
    snapshots = []
    for wrong in ("x", "xx"):
        conn = _connection()
        conn.connect()
        with pytest.raises(ReplayMismatchError):
            conn.write(wrong)
        snapshots.append(conn.snapshot().to_dict())

    assert snapshots[0]["run_sha256"] != snapshots[1]["run_sha256"]
    assert snapshots[0]["writes"] == [
        {
            "seq": 2,
            "observed_at_ms": 0,
            "incarnation": 1,
            "bytes_requested": 2,
            "expected_bytes": 7,
            "disposition": "simulated_payload_mismatch",
        }
    ]
    assert "status" not in _all_string_values(snapshots)
    assert "payload" not in _all_mapping_keys(snapshots)
    assert "digest" not in _all_mapping_keys(snapshots)


def test_early_write_is_a_terminal_mismatch_not_a_clock_shortcut() -> None:
    payload = _fixture_dict()
    payload["events"][1]["at_ms"] = 10
    payload["events"][2]["at_ms"] = 11
    payload["events"][3]["at_ms"] = 12
    payload["events"][4]["at_ms"] = 13
    conn = ReplayConnection(_transcript(payload))
    conn.connect()

    assert conn.blocked_on == "time"
    with pytest.raises(ReplayMismatchError):
        conn.write("status")
    assert conn.virtual_ms == 0
    assert conn.cursor == 1
    assert conn.state is ConnectionState.ERROR


def test_callback_snapshot_duplicate_and_first_removal_semantics() -> None:
    conn = _connection()
    observed: list[tuple[str, str]] = []

    def second(line: str) -> None:
        observed.append(("second", line))

    def first(line: str) -> None:
        observed.append(("first", line))
        conn.remove_line_callback(second)

    conn.on_line(first)
    conn.on_line(second)
    conn.on_line(second)
    conn.remove_line_callback(second)  # removes only the first equal registration
    conn.connect()
    conn.write("status")
    conn.advance_to(2)

    assert [name for name, _line in observed] == ["first", "second", "first"]


def test_callback_removal_uses_identity_not_hostile_equality() -> None:
    class HostileCallback:
        def __call__(self, _line: str) -> None:
            pass

        def __eq__(self, _other) -> bool:
            raise AssertionError("callback equality must not execute")

    def other(_line: str) -> None:
        pass

    conn = _connection()
    hostile = HostileCallback()
    conn.on_line(hostile)
    conn.remove_line_callback(other)
    conn.remove_line_callback(hostile)


def test_ordinary_callback_error_is_isolated_but_makes_pass_impossible() -> None:
    conn = _connection()
    received: list[str] = []

    def broken(_line: str) -> None:
        raise RuntimeError("sensitive observer detail")

    conn.on_line(broken)
    conn.on_line(received.append)
    conn.connect()
    conn.write("status")
    conn.advance_to(3)

    assert len(received) == 2  # one observer cannot starve the next observer
    assert conn.state is ConnectionState.DISCONNECTED
    assert conn.run_status == "callback_failed"
    assert conn.complete is False
    evidence = conn.snapshot().to_dict()
    evidence_text = json.dumps(evidence, sort_keys=True)
    assert "sensitive observer detail" not in evidence_text
    assert "RuntimeError" not in evidence_text

    # A completed failure is immutable under later cleanup or accidental calls.
    before = conn.snapshot().run_sha256
    conn.cancel()
    conn.disconnect()
    with pytest.raises(ReplayStateError):
        conn.write("status")
    assert conn.run_status == "callback_failed"
    assert conn.snapshot().run_sha256 == before


def test_observer_error_evidence_is_bounded_but_total_is_exact() -> None:
    payload = _fixture_dict()
    payload["events"] = [{"seq": 1, "at_ms": 0, "kind": "connect"}]
    for seq in range(2, 19):
        payload["events"].append(
            {
                "seq": seq,
                "at_ms": seq,
                "kind": "rx_line",
                "text": f"DEVICE/1 sample={seq}",
            }
        )
    payload["events"].append(
        {"seq": 19, "at_ms": 19, "kind": "disconnect", "reason": "scripted_end"}
    )
    conn = ReplayConnection(_transcript(payload))

    def broken(_line: str) -> None:
        raise RuntimeError("not retained")

    for _ in range(64):
        conn.on_line(broken)
    conn.connect()
    conn.drain()
    evidence = conn.snapshot().to_dict()
    assert evidence["observer_error_count"] == 17 * 64
    assert len(evidence["observer_errors"]) == 1_024
    assert evidence["observer_error_overflow"] == 64
    assert conn.run_status == "callback_failed"


def test_base_exception_settles_interrupted_state_before_propagating() -> None:
    from src.core.device_manager import DeviceManager
    from src.models.device import Device

    class StopReplay(BaseException):
        pass

    conn = _connection()
    states: list[ConnectionState] = []
    conn.on_state_change(states.append)

    def interrupt(_line: str) -> None:
        raise StopReplay()

    conn.on_line(interrupt)
    conn.connect()
    dm = DeviceManager()
    dev = Device(port=conn.port, name="Synthetic", firmware="lxveos")
    dm.attach_connection(dev, conn)
    assert dev.connected is True
    conn.write("status")
    with pytest.raises(StopReplay):
        conn.advance_to(1)

    assert conn.cursor == 3  # at-most-once: event was claimed before callback fanout
    assert conn.state is ConnectionState.ERROR
    assert conn.run_status == "interrupted"
    assert conn.complete is False
    assert states[-1] is ConnectionState.ERROR
    assert dev.connected is False
    evidence = conn.snapshot().to_dict()
    assert evidence["observer_error_count"] >= 1
    assert evidence["observer_errors"][0]["seq"] == 3
    assert evidence["observer_errors"][0]["category"] == "line"
    evidence_text = json.dumps(evidence, sort_keys=True)
    assert "StopReplay" not in evidence_text
    with pytest.raises(ReplayStateError):
        conn.advance_to(2)


def test_reentrant_drive_fails_closed_without_emitting_later_lines() -> None:
    conn = _connection()
    delivered: list[str] = []

    def reenter(line: str) -> None:
        delivered.append(line)
        conn.advance_by(0)

    conn.on_line(reenter)
    conn.connect()
    conn.write("status")
    assert conn.advance_to(2) == 1
    assert len(delivered) == 1
    assert conn.state is ConnectionState.ERROR
    assert conn.run_status == "operator_stopped"
    assert conn.cursor == 3


def test_concurrent_cancel_is_requested_and_settled_by_drive_owner() -> None:
    conn = _connection()
    entered = threading.Event()
    release = threading.Event()
    delivered: list[str] = []
    failures: list[BaseException] = []

    def blocking(line: str) -> None:
        delivered.append(line)
        entered.set()
        assert release.wait(2)

    def drive() -> None:
        try:
            conn.advance_to(2)
        except BaseException as exc:
            failures.append(exc)

    conn.on_line(blocking)
    conn.connect()
    conn.write("status")
    worker = threading.Thread(target=drive)
    worker.start()
    assert entered.wait(2)
    assert conn.cancel() is None
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert failures == []
    assert len(delivered) == 1
    assert conn.cursor == 3
    assert conn.state is ConnectionState.DISCONNECTED
    assert conn.run_status == "operator_stopped"


def test_cancel_after_atomic_drive_release_settles_before_public_drive_returns() -> None:
    class GatedFinishReplayConnection(ReplayConnection):
        gate_enabled = False

        def _finish_drive(self) -> None:
            super()._finish_drive()
            if self.gate_enabled:
                entered.set()
                assert release.wait(2)

    entered = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []
    conn = GatedFinishReplayConnection(load_transcript(FIXTURE, fixture_root=FIXTURE_ROOT))
    conn.gate_enabled = True

    def drive() -> None:
        try:
            conn.disconnect()  # initial disconnected cleanup is otherwise a no-op
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=drive)
    worker.start()
    assert entered.wait(2)
    conn.cancel()
    assert conn.run_status == "operator_stopped"
    assert conn.snapshot().terminal_reason == "cancelled"
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert failures == []
    assert conn.cursor == 0


def test_terminal_callback_snapshots_stay_in_flight_through_complete_fanout() -> None:
    conn = _connection()
    conn.connect()
    callback_snapshots = []

    def snapshot_first(_state: ConnectionState) -> None:
        callback_snapshots.append(conn.snapshot())

    def fail_second(_state: ConnectionState) -> None:
        raise RuntimeError("not retained")

    conn.on_state_change(snapshot_first)
    conn.on_state_change(fail_second)
    conn.cancel()

    assert len(callback_snapshots) == 1
    transient = callback_snapshots[0]
    settled = conn.snapshot()
    assert transient.in_flight is True
    assert transient.observer_error_count == 0
    assert settled.in_flight is False
    assert settled.observer_error_count == 1
    assert transient.run_sha256 != settled.run_sha256
    assert settled.run_status == "operator_stopped"


def test_cancel_before_final_status_refresh_cannot_become_pass() -> None:
    class GatedRefreshReplayConnection(ReplayConnection):
        gate_enabled = False
        gate_used = False

        def _refresh_status(self) -> None:
            if self.gate_enabled and not self.gate_used and self.cursor == 5:
                self.gate_used = True
                entered.set()
                assert release.wait(2)
            super()._refresh_status()

    entered = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []
    conn = GatedRefreshReplayConnection(load_transcript(FIXTURE, fixture_root=FIXTURE_ROOT))
    conn.connect()
    conn.write("status")
    conn.gate_enabled = True

    def drive() -> None:
        try:
            conn.advance_to(3)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=drive)
    worker.start()
    assert entered.wait(2)
    conn.cancel()
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert failures == []
    evidence = conn.snapshot()
    assert evidence.cursor == 5
    assert evidence.complete is False
    assert evidence.run_status == "operator_stopped"
    assert evidence.terminal_reason == "cancelled"
    assert evidence.in_flight is False


def test_competing_drive_request_is_settled_at_owner_release() -> None:
    class GatedFinishReplayConnection(ReplayConnection):
        gate_enabled = False

        def _finish_drive(self) -> None:
            if self.gate_enabled:
                entered.set()
                assert release.wait(2)
            super()._finish_drive()

    entered = threading.Event()
    release = threading.Event()
    owner_failures: list[BaseException] = []
    conn = GatedFinishReplayConnection(load_transcript(FIXTURE, fixture_root=FIXTURE_ROOT))
    conn.gate_enabled = True

    def owner() -> None:
        try:
            conn.disconnect()
        except BaseException as exc:
            owner_failures.append(exc)

    worker = threading.Thread(target=owner)
    worker.start()
    assert entered.wait(2)
    with pytest.raises(ReplayStateError):
        conn.connect()
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert owner_failures == []
    assert conn.run_status == "operator_stopped"
    assert conn.state is ConnectionState.ERROR
    assert conn.snapshot().terminal_reason == "concurrent_or_reentrant_drive"


def test_pending_competing_drive_wins_before_manual_terminalization() -> None:
    class GatedTerminalReplayConnection(ReplayConnection):
        def _terminalize(self, status: str, reason: str, state: ConnectionState) -> None:
            if reason == "manual_disconnect":
                entered.set()
                assert release.wait(2)
            super()._terminalize(status, reason, state)

    entered = threading.Event()
    release = threading.Event()
    owner_failures: list[BaseException] = []
    conn = GatedTerminalReplayConnection(load_transcript(FIXTURE, fixture_root=FIXTURE_ROOT))
    conn.connect()

    def owner() -> None:
        try:
            conn.disconnect()
        except BaseException as exc:
            owner_failures.append(exc)

    worker = threading.Thread(target=owner)
    worker.start()
    assert entered.wait(2)
    with pytest.raises(ReplayStateError):
        conn.advance_by(0)
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert owner_failures == []
    evidence = conn.snapshot()
    assert evidence.run_status == "operator_stopped"
    assert evidence.final_state == ConnectionState.ERROR.value
    assert evidence.terminal_reason == "concurrent_or_reentrant_drive"


def test_cancel_before_state_transition_emits_no_post_cancel_connected_state() -> None:
    class GatedTransitionReplayConnection(ReplayConnection):
        def _transition(self, state: ConnectionState, seq: int | None) -> None:
            if state is ConnectionState.CONNECTED:
                entered.set()
                assert release.wait(2)
            super()._transition(state, seq)

    entered = threading.Event()
    release = threading.Event()
    owner_failures: list[BaseException] = []
    states: list[ConnectionState] = []
    conn = GatedTransitionReplayConnection(load_transcript(FIXTURE, fixture_root=FIXTURE_ROOT))
    conn.on_state_change(states.append)

    def owner() -> None:
        try:
            conn.connect()
        except BaseException as exc:
            owner_failures.append(exc)

    worker = threading.Thread(target=owner)
    worker.start()
    assert entered.wait(2)
    conn.cancel()
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert len(owner_failures) == 1
    assert isinstance(owner_failures[0], ReplayStateError)
    assert ConnectionState.CONNECTED not in states
    assert states == [ConnectionState.CONNECTING, ConnectionState.DISCONNECTED]
    assert conn.run_status == "operator_stopped"
    assert conn.snapshot().terminal_reason == "cancelled"


def test_cancel_between_automatic_stop_check_and_claim_admits_no_event() -> None:
    class GatedReplayConnection(ReplayConnection):
        gate_enabled = False
        gate_used = False

        def _settle_stop_request(self) -> None:
            super()._settle_stop_request()
            if self.gate_enabled and not self.gate_used:
                self.gate_used = True
                entered.set()
                assert release.wait(2)

    entered = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []
    transcript = load_transcript(FIXTURE, fixture_root=FIXTURE_ROOT)
    conn = GatedReplayConnection(transcript)
    conn.connect()
    conn.write("status")
    conn.gate_enabled = True

    def drive() -> None:
        try:
            conn.advance_to(2)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=drive)
    worker.start()
    assert entered.wait(2)
    conn.cancel()
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert failures == []
    assert conn.cursor == 2
    assert conn.virtual_ms == 2
    assert conn.state is ConnectionState.DISCONNECTED
    assert conn.run_status == "operator_stopped"
    assert conn.snapshot().terminal_reason == "cancelled"


def test_base_exception_from_error_observer_overrides_mismatch_as_interrupted() -> None:
    class StopReplay(BaseException):
        pass

    def stop(_error: Exception) -> None:
        raise StopReplay()

    conn = _connection()
    conn.connect()
    conn.on_error(stop)
    with pytest.raises(StopReplay):
        conn.write("wrong")
    assert conn.run_status == "interrupted"
    assert conn.state is ConnectionState.ERROR
    assert conn.cursor == 1


def test_callback_registration_is_bounded() -> None:
    conn = _connection()

    def callback(_line: str) -> None:
        pass

    for _ in range(64):
        conn.on_line(callback)
    with pytest.raises(ReplayStateError):
        conn.on_line(callback)


def test_scripted_disconnect_and_reconnect_increment_incarnation() -> None:
    payload = _fixture_dict()
    payload["events"] = [
        {"seq": 1, "at_ms": 0, "kind": "connect"},
        {"seq": 2, "at_ms": 10, "kind": "disconnect", "reason": "link_loss"},
        {"seq": 3, "at_ms": 20, "kind": "connect"},
        {
            "seq": 4,
            "at_ms": 21,
            "kind": "rx_line",
            "text": "LXVEOS/1 status caps=0x003 arm=safe tx=0 synthetic=1",
        },
        {"seq": 5, "at_ms": 22, "kind": "disconnect", "reason": "scripted_end"},
    ]
    conn = ReplayConnection(_transcript(payload))
    states: list[ConnectionState] = []
    conn.on_state_change(states.append)

    assert conn.incarnation == 0
    conn.connect()
    assert conn.incarnation == 1
    conn.advance_to(20)
    assert conn.state is ConnectionState.DISCONNECTED
    assert conn.blocked_on == "connect"
    assert conn.virtual_ms == 20

    conn.connect()
    assert conn.incarnation == 2
    conn.advance_to(22)
    assert conn.run_status == "passed"
    assert states == [
        ConnectionState.CONNECTING,
        ConnectionState.CONNECTED,
        ConnectionState.DISCONNECTED,
        ConnectionState.CONNECTING,
        ConnectionState.CONNECTED,
        ConnectionState.DISCONNECTED,
    ]


def test_manual_disconnect_is_an_explicit_incomplete_terminal_outcome() -> None:
    conn = _connection()
    conn.connect()
    conn.disconnect()

    assert conn.state is ConnectionState.DISCONNECTED
    assert conn.cursor == 1
    assert conn.run_status == "operator_stopped"
    assert conn.complete is False
    conn.disconnect()  # already-disconnected cleanup remains idempotent


def test_manual_disconnect_after_link_loss_terminalizes_unfinished_replay() -> None:
    payload = _fixture_dict()
    payload["events"] = [
        {"seq": 1, "at_ms": 0, "kind": "connect"},
        {"seq": 2, "at_ms": 1, "kind": "disconnect", "reason": "link_loss"},
        {"seq": 3, "at_ms": 2, "kind": "connect"},
        {"seq": 4, "at_ms": 3, "kind": "disconnect", "reason": "scripted_end"},
    ]
    conn = ReplayConnection(_transcript(payload))
    conn.connect()
    conn.advance_to(1)
    assert conn.state is ConnectionState.DISCONNECTED
    assert conn.blocked_on == "connect"

    conn.disconnect()
    assert conn.run_status == "operator_stopped"
    assert conn.snapshot().terminal_reason == "manual_disconnect"
    assert conn.blocked_on == "terminal"


def test_evidence_is_deterministic_detached_and_payload_free() -> None:
    def completed_evidence():
        conn = _connection()
        conn.connect()
        conn.write("status")
        conn.advance_to(3)
        return conn.snapshot()

    first = completed_evidence()
    second = completed_evidence()
    assert first.run_sha256 == second.run_sha256
    assert first.to_dict() == second.to_dict()

    evidence = first.to_dict()
    assert evidence["schema"] == "CyberControllerReplayEvidence@1"
    assert evidence["integrity_model"] == "unauthenticated_checksum"
    assert evidence["run_status"] == "passed"
    assert evidence["in_flight"] is False
    assert evidence["simulated"] is True
    assert len(evidence["source_sha256"]) == 64
    assert len(evidence["transcript_sha256"]) == 64
    assert len(evidence["run_sha256"]) == 64
    assert [
        outcome["outcome"] for outcome in evidence["events"] if outcome["direction"] == "rx"
    ] == ["simulated_emission_claimed", "simulated_emission_claimed"]
    forbidden_keys = {
        "text",
        "line",
        "command",
        "payload",
        "wire_sha256",
        "payload_sha256",
        "exception",
        "exception_type",
        "exception_message",
    }
    assert _all_mapping_keys(evidence).isdisjoint(forbidden_keys)
    evidence_strings = _all_string_values(evidence)
    for raw_line in [event["text"] for event in _fixture_dict()["events"] if "text" in event]:
        assert raw_line not in evidence_strings

    # A detached snapshot cannot be mutated through a later returned mapping.
    evidence["run_status"] = "tampered"
    assert first.to_dict()["run_status"] == "passed"


def test_callback_snapshots_are_marked_in_flight_until_status_settles() -> None:
    conn = _connection()
    snapshots = []
    conn.on_state_change(lambda _state: snapshots.append(conn.snapshot()))
    conn.connect()
    conn.write("status")
    conn.advance_to(3)

    assert snapshots
    assert all(item.in_flight for item in snapshots)
    assert snapshots[0].run_status == "running"
    assert snapshots[-1].cursor == snapshots[-1].total_events
    assert snapshots[-1].blocked_on == "settling"
    assert snapshots[-1].complete is False
    settled = conn.snapshot()
    assert settled.in_flight is False
    assert settled.blocked_on == "done"
    assert settled.complete is True


def test_device_manager_and_cross_comm_ingest_without_constructing_serial(monkeypatch) -> None:
    import serial

    from src.core.cross_comm_hub import CrossCommHub
    from src.core.device_manager import DeviceManager
    from src.models.device import Device

    def forbidden_serial(*_args, **_kwargs):
        raise AssertionError("a deterministic replay must never construct serial.Serial")

    monkeypatch.setattr(serial, "Serial", forbidden_serial)

    transcript = load_transcript(FIXTURE, fixture_root=FIXTURE_ROOT)
    conn = ReplayConnection(transcript)
    conn.connect()
    dm = DeviceManager()
    hub = CrossCommHub(dm)
    assert hub.router.list_rules() == []  # never inject synthetic targets into a live rule set
    dev = Device(port=conn.port, name="Synthetic LxveOS", firmware="lxveos")
    dm.attach_connection(dev, conn)

    assert hub.send_to_port(conn.port, "status") is True
    conn.advance_to(2)
    assert dev.runtime_capabilities == frozenset({"wifi", "ble"})
    assert dev.arm_state == "safe"

    targets = hub.pool.all()
    assert len(targets) == 1
    assert targets[0].mac.lower() == "02:00:00:00:00:01"
    assert targets[0].ssid == "fixture-ap"
    # Pre-existing seam truth: LxveOS preserves the wire key as ``ch`` while
    # TargetIngestor currently reads ``channel``.  Replay must not invent a value.
    assert targets[0].channel == 0
    assert targets[0].rssi == -60
    assert targets[0].device_source == conn.port

    conn.advance_to(3)
    assert dev.connected is False
    assert conn.run_status == "passed"
    hub.close()
