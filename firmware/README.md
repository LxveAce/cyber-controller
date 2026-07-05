# Wireless node firmware

Firmware for the ESP32 boards that make up Cyber Controller's wireless node system. The host side (sealing,
anti-replay, provisioning, the Nodes UI) already ships and is covered by tests; this is the other end of the
link.

- **[`relay/`](relay/)** — the USB-tethered gateway. Bridges the host's serial link to ESP-NOW. Holds no
  keys and does no crypto; it just moves frames. Built.
- **[`node/`](node/)** — the remote sensor board. Holds its own key + `node_id`, unseals commands (dropping
  forged or replayed frames) and seals its output with on-device AES-256-GCM. Built.

The wire contract both ends implement is in **[`PROTOCOL.md`](PROTOCOL.md)** — read that first. It matches
`src/core/node_crypto.py` byte for byte.

## Status — honest

Both sketches are a **real implementation** of the W1.0 protocol, but they have **not been compiled or
flashed here**: this build environment has no `arduino-cli`/ESP32 toolchain, and no board was available to
test on. Treat them as reviewed-against-spec, not hardware-validated. What *is* verified: the protocol
contract each sketch implements — the relay's base64↔frame transcode and the node's AES-256-GCM envelope +
anti-replay — is unit-tested in Python against the real host crypto (`tests/test_relay_frame_roundtrip.py`,
`tests/test_node_contract.py`). Before relying on the firmware on hardware:

1. Compile against ESP32 Arduino core 2.x
   (`arduino-cli compile --fqbn esp32:esp32:esp32 relay` and `... node`).
2. Provision the node: replace the placeholder `NODE_KEY`/`NODE_ID` in `node/node.ino` with a real
   per-node key from the host (`src/core/node_provision.py`) — never ship the demo key.
3. Flash a relay + a node and confirm a round-trip frame against the host `NodeLink`.
4. On ESP32 core 3.x, update the ESP-NOW receive-callback signature (noted in each sketch).

Once a round-trip is confirmed on hardware, this note should be updated to say so.
