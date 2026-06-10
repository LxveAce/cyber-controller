# Security Policy

Cyber Controller drives real RF-attack, flashing, and anti-forensic hardware. It is built for
**authorized** security testing, education, and CTF use only (see the
[disclaimer](https://esp32marauder.com/disclaimer.html)). The codebase is hardened accordingly.

## Reporting a vulnerability

Email **lxveace@proton.me** with details and reproduction steps. Please do **not** open public
issues for security-sensitive reports. You will receive an acknowledgement; coordinated disclosure
is appreciated.

## Hardening summary

### Web remote (`src/ui/web/`)
- **Authenticated WebSockets** — the SocketIO `connect` handler rejects any unauthenticated session
  and validates a per-session CSRF/connection token; every `subscribe_serial` / `send_command` event
  re-checks the session and validates the target port against the device registry.
- **Local by default** — binds `127.0.0.1`; LAN exposure requires the explicit `CC_WEB_ALLOW_LAN=1`
  opt-in, and TLS via `CC_WEB_CERT` / `CC_WEB_KEY` is encouraged.
- **No default credentials** — a strong one-time password is generated and printed if `CC_WEB_PASS`
  is unset. Credentials are verified in **constant time** against a salted scrypt hash.
- **CSRF** tokens on state-changing POSTs and the socket handshake; **per-IP rate limiting** on auth
  and command/flash actions; **CORS allowlist** (never `*`); `SameSite=Strict` + `HttpOnly` cookies;
  stable file-persisted (`0600`) secret key; strict `Content-Security-Policy` and security headers;
  request body size cap.
- **XSS-safe rendering** — over-the-air scan data (SSIDs/MACs) is rendered via DOM `textContent`,
  never `innerHTML` string concatenation.

### Firmware supply chain (`src/core/flash_core.py`, `firmware_vault.py`)
- **SSRF-safe downloads** — pinned to an HTTPS GitHub host allowlist with **redirect validation**
  (a 302 cannot bounce the downloader to a metadata/LAN endpoint); body **size-capped**.
- **Path-traversal guards** on every remote asset / bundle filename, with realpath containment.
- **SHA-256 integrity pinning** — profiles may pin `firmware_sha256`; a mismatch hard-fails and the
  download is deleted. The arbitrary `assets[0]` fallback was removed (name-matched `.bin` required).
- **TOCTOU-safe bundle flashing** — Suicide-Marauder bundles are verified per file, staged into a
  `0700` tempdir, and re-hashed before a single atomic `write_flash`; suicide-schema bundles refuse
  to flash without a SHA-256 for every file (no trust-on-first-use downgrade).

### Encryption & secrets (`src/security/`)
- **AES-256-GCM** (scrypt KDF) is the only cipher and **fails closed** — there is no unauthenticated
  XOR fallback, and `cryptography` is a mandatory dependency.
- Secret-bearing files are written with `0600` permissions.

### Command-injection defenses
- `SerialConnection.write` rejects embedded newlines/control characters so one logical command can
  never expand into many.
- `AutoRouter` substitutes only fixed `{mac}`/`{ssid}`/`{channel}` placeholders (no `str.format` on
  untrusted data) and sanitizes/validates over-the-air values before they reach the serial port.

### Auditing
- A tamper-evident SHA-256 hash-chain **audit trail** records flash, serial-command, and auth events.

## Supported versions

The latest `master` is the supported version. Security fixes are applied to `master`.
