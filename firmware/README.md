# Wireless node firmware

Firmware for the ESP32 boards that make up Cyber Controller's wireless node system. The host side (sealing,
anti-replay, provisioning, the Nodes UI) already ships and is covered by tests; this is the other end of the
link.

- **[`relay/`](relay/)** — the USB-tethered gateway. Bridges the host's serial link to ESP-NOW. Holds no
  keys and does no crypto; it just moves frames. Built.
- **`node/`** — the remote sensor board. Holds its own key + `node_id`, unseals commands and seals its
  output. Coming next.

The wire contract both ends implement is in **[`PROTOCOL.md`](PROTOCOL.md)** — read that first. It matches
`src/core/node_crypto.py` byte for byte.

## Status — honest

This firmware is a **real implementation** of the W1.0 protocol, but it has **not been compiled or flashed
here**: this build environment has no `arduino-cli`/ESP32 toolchain, and no board was available to test on.
Treat it as reviewed-against-spec, not hardware-validated. Before relying on it:

1. Compile it against ESP32 Arduino core 2.x (`arduino-cli compile --fqbn esp32:esp32:esp32 relay`).
2. Flash a board and confirm a round-trip frame against the host `NodeLink`.
3. On ESP32 core 3.x, update the ESP-NOW receive-callback signature (noted in `relay/relay.ino`).

Once a round-trip is confirmed on hardware, this note should be updated to say so.
