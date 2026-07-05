# Wireless node protocol — W1.0

This is the on-the-wire contract between the Cyber Controller host and its wireless nodes. The host side is
already implemented and tested (`src/core/node_crypto.py`, `src/core/node_link.py`); this document is the
spec the **firmware** in this directory implements so both ends agree byte-for-byte.

## Roles

```
  ┌────────┐   USB serial    ┌────────┐   ESP-NOW (2.4 GHz)   ┌────────┐
  │  host  │ ◀────────────▶ │ relay  │ ◀──────────────────▶ │  node  │
  │ (app)  │  base64 lines   │(gateway)│   raw frame bytes    │(sensor)│
  └────────┘                 └────────┘                       └────────┘
```

- **host** — the Cyber Controller app. Holds a distinct 32-byte key per node. Seals every command and
  unseals every reply (`NodeLink`).
- **relay** — an ESP32 tethered to the host over USB. A **dumb bridge**: it moves frames between the serial
  link and ESP-NOW and does **no crypto**. It never holds a key and cannot read node traffic.
- **node** — a remote ESP32. Holds its own 32-byte key + `node_id`. Unseals commands, seals its firmware's
  output.

Because encryption is **end-to-end between host and node**, the relay can broadcast every downlink frame to
all nodes: only the node holding the matching key can unseal it; every other node's tag check fails and it
drops the frame. That is why the relay needs no routing table and no secrets.

## Sealed frame (host ↔ node, end-to-end)

Big-endian, matching `node_crypto.py` exactly:

```
+--------+------------+-----------+-------------+----------------------------+
| ver u8 | node_id u16| epoch u32 | counter u64 | AES-256-GCM(ciphertext‖tag)|
+--------+------------+-----------+-------------+----------------------------+
 \________________ 15-byte header (authenticated as AAD) _________________/
```

| Field | Bytes | Notes |
|-------|-------|-------|
| version | 1 | `1` for W1.0 |
| node_id | 2 | identifies the node; authenticated, **not** part of the nonce |
| epoch | 4 | high half of the GCM nonce |
| counter | 8 | low half of the GCM nonce; monotonic per key |
| ciphertext+tag | n+16 | AES-256-GCM; 16-byte tag appended |

- **Cipher:** AES-256-GCM. 32-byte per-node key, 16-byte tag.
- **Nonce (96-bit):** `epoch(4) ‖ counter(8)`. Never reuse a (key, nonce) pair — the sender increments the
  counter every frame and rotates the epoch on overflow.
- **AAD:** the whole 15-byte header is bound into the tag, so no header field can be altered without failing
  authentication.
- **Overhead:** header 15 + tag 16 = **31 bytes**. With the ESP-NOW 250-byte payload budget a single frame
  carries up to **219 bytes** of plaintext. Anything larger is fragmented by the host (not in W1.0).
- **Anti-replay:** the receiver keeps a sliding-window guard (`ReplayWindow`) checked only after the tag
  verifies. **Each node must hold its own key** — sharing a key across nodes would reuse nonces and break
  GCM.

The plaintext is just the node firmware's own serial line(s); the host splits on `\r`/`\n` and surfaces each
non-empty line, exactly like a wired serial device.

## Transport framing

**Serial (host ↔ relay):** one frame per line — `base64(frame)` followed by `\n`. Base64 keeps the link
control-character free (the host's serial guard rejects embedded control chars), and lets the host frame on
newlines. The relay base64-**decodes** host→node lines and base64-**encodes** node→host frames.

**ESP-NOW (relay ↔ node):** the **raw** frame bytes (≤ 250), no base64 — ESP-NOW carries binary and has its
own length. The relay adds/strips only the base64 layer; the sealed frame itself is untouched end to end.

## Provisioning

Keys are generated and stored on the host (`src/core/node_provision.py`) and flashed onto each node
out-of-band. Keys never travel over the air. A node ships with its `node_id` and 32-byte key; the host
records the same pair. See `firmware/node/` for where the key lands in the node sketch.
