# Deterministic transcript replay

Cyber Controller's transcript replayer is a parser-level simulator for repeatable
tests. It consumes a strictly validated JSON scenario, advances only when the test
drives its manual clock, and delivers already-framed text lines to ordinary
connection callbacks.

It never opens serial ports, sockets, subprocesses, Docker, or hardware. A replay
snapshot is marked `parser_level_simulation`, `simulated=true`, and
`hardware_evidence=false`. It must not be presented as HIL, acquisition, incident,
boot, radio, or physical-delivery evidence.

## Safe use

Load fixtures only from a deliberate trusted root:

```python
from pathlib import Path

from src.core.transports import ReplayConnection, load_transcript

root = Path("tests/fixtures/transports")
transcript = load_transcript("lxveos-passive-v1.json", fixture_root=root)
connection = ReplayConnection(transcript)

lines = []
connection.on_line(lines.append)
connection.connect()
connection.write("status")       # matches an explicit simulated expectation
connection.advance_to(3)         # no sleep and no wall clock

assert connection.complete
evidence = connection.snapshot().to_dict()
```

`load_transcript` rejects paths outside its root, URL/device/UNC/ADS syntax,
traversal, expansion/glob syntax, non-files, and symlink/reparse traversal. For
generated in-memory test inputs, use `TransportTranscript.from_bytes(exact_bytes)`.

Do not attach a replay connection to Cyber Controller's live process-wide
`DeviceManager`. A synthetic discovery can enter `TargetPool`, and configured
`AutoRouter` rules could then address unrelated real devices. An integration test
must build a fresh, isolated manager/hub with no routing rules, never call probe,
and use an opaque `sim://` port. Production registration is intentionally not
wired in version 1.

## Version 1 contract

The exact top-level shape is:

```json
{
  "schema": "CyberControllerTransportTranscript@1",
  "scenario_id": "example-scenario",
  "device": {
    "port": "sim://example/device-001",
    "firmware": "example",
    "name": "Synthetic example",
    "simulated": true
  },
  "connection": {
    "mode": "text",
    "baud": 115200,
    "encoding": "utf-8",
    "line_ending": "\n"
  },
  "basis": null,
  "events": [
    {"seq": 1, "at_ms": 0, "kind": "connect"},
    {"seq": 2, "at_ms": 0, "kind": "expect_text_write", "text": "status"},
    {"seq": 3, "at_ms": 1, "kind": "rx_line", "text": "DEVICE/1 ok synthetic=1"},
    {"seq": 4, "at_ms": 2, "kind": "disconnect", "reason": "scripted_end"}
  ]
}
```

The loader rejects unknown or duplicate keys at every level, JSON floats and
non-finite values, booleans used as integers, oversized integers, invalid UTF-8,
a BOM, escaped surrogate code points, controls, unbalanced lifecycle events, and
all configured byte/count/time limit violations. There are no includes, imports,
URLs, output paths, environment expansion, YAML, or executable expressions.

Events use contiguous one-based `seq` values and nondecreasing integer `at_ms`
values. The first event is a connect barrier at 0 ms. The final event is a
`scripted_end` disconnect. Receive and expected-write events may occur only while
the scripted connection is connected. A later connect after `link_loss` or
`device_reset` begins a new numbered incarnation; `scripted_end` is terminal and
is rejected anywhere except the last event.

`connect()` and an expected write are action barriers. `advance_to()` retains the
requested virtual time but never crosses either barrier. `drain()` advances only
as far as needed through receive/disconnect events and also stops at a barrier.
There are no sleeps, timers, or background threads.
Relative clock advances acquire exclusive drive ownership before deriving their
target, so overlapping calls fail closed instead of silently losing a delta.

Text writes use the same command normalization as the real text connection:
trailing CR/LF characters are removed and exactly the fixture-pinned terminator is
appended. Embedded C0/DEL controls are rejected before the simulated write. An
early, extra, disconnected, or unequal write is terminal, leaves the expectation
unconsumed, and reports only safe reason/length metadata. The raw runtime input is
bounded before terminator normalization, so newline padding cannot bypass the
command limit.

## Evidence boundary

`ReplayConnection.snapshot()` returns a frozen `ReplayEvidence`. Its JSON-safe
copy contains exact source-byte and canonical-transcript SHA-256 identities,
cursor/time/incarnation/state, an `in_flight` marker, payload-free event outcomes,
observer-error locations, and a domain-separated deterministic run checksum. It
contains no command, line, payload, exception message/type, wall time, filesystem
path, or physical delivery claim.

The run checksum is explicitly `integrity_model=unauthenticated_checksum`. It
detects ordinary serialization differences and makes repeatable test comparisons
easy; it is not a signature, tamper-proof record, or attestation boundary against
code running in the same Python process. Frozen dataclasses and admission
revalidation defend normal use and accidental mutation, not a caller deliberately
using private attributes or `object.__setattr__`. Obtain independently signed
artifacts outside the process for an actual attestation design.

An optional `basis` object is preserved as declared provenance with
`verification=declared_unverified`. Its digest is not independently verified by
the replayer.

A run passes only after every event is consumed, the final scripted disconnect is
reached, and no callback failed or runtime input was rejected. Ordinary callback
exceptions are isolated so the next observer still runs, but make PASS impossible.
Rejected inputs settle as `input_rejected` if the valid transcript is later fully
consumed. A `BaseException`, concurrent drive, re-entrant drive, unexpected write,
cancellation, or manual early disconnect settles a terminal non-PASS state while
retaining partial evidence.

Mismatch evidence records only the reason and expected/observed byte lengths. It
therefore distinguishes different lengths in the run hash while intentionally
making same-length wrong payloads indistinguishable. Observer callbacks execute
outside the metadata lock but under exclusive non-blocking drive ownership;
inspection is allowed, while nested or concurrent driving is rejected.
Snapshots are point-in-time records. A snapshot taken by a callback reports
`in_flight=true`; the final disconnect callback can therefore show
`blocked_on=settling` and `complete=false` until callback fanout and final status
calculation finish.

An RX outcome is named `simulated_emission_claimed`, not `delivered`: it means the
replay claimed the scripted line for callback fanout. Replay PASS proves complete
transcript progression and that no callback exception reached the replay. It does
not prove that a parser, event bus, router, or model update succeeded because a
downstream component may contain its own errors. Integration tests must assert the
expected downstream state separately.

## Intentional non-goals

Version 1 does not emulate incremental byte decoding, invalid UTF-8 replacement,
CR/LF coalescing, empty-line dropping, the real 64 KiB framing buffer, raw/binary
streams, serial timeouts, short/zero/invalid writes, flush uncertainty, adapter
reset behavior, or host/firmware timing. It therefore exposes neither
`write_receipt` nor raw-byte methods. Those require a separate contract and must
not borrow hardware-delivery labels from `SerialConnection`.
