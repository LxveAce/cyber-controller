# Cyber Controller — Future Roadmap

Where the flagship goes next. Grounded in the current shipped reality (v1.6.4: Qt/Tk/TUI/Web front-ends,
5 flash backends, 12 protocol parsers, 31 profile JSONs, cross-comm AutoRouter, dead-man-switch submodule)
and the planned support advertised on cybercontroller.org. **Principle that governs every line below:
reliability over reach — a target is only marketed as *supported* once it is validated on real hardware.**
Code can land ahead of validation, but it ships flagged `HW-validation pending`, never as "supported."

Status legend: ✅ shipped · 🚧 in progress · 📋 planned · 🔬 needs hardware to validate · 🔑 owner decision

---

## Near term — stabilize & release

- 📋 **Finish the reliability/consistency backlog:** bundle-manifest regression test (guard that every
  `resource_path()` target exists and matches a `build.py --add-data` entry — catches the next
  silent-frozen-crash and the stale `src/config/missions` line), remaining inline-style → `theme.colors`
  token routing, and the `mission.py` decision below.
- 🔑 **Windows code-signing** (OV/EV cert) to retire the SmartScreen prompt; **firmware-profile count**
  reconciled to one canonical definition across README + all three sites.
- 🔑 **`mission.py` scaffolding:** either build the mission planner on it (below) or delete the dead
  module + the phantom `src/config/missions` bundle line.

## Mid term — *more support* (the CC-website roadmap)

### Flash backends & target hardware
Today: esptool · qFlipper · ADB · SD-image · AmebaD(rtl8720). Planned, each **🔬 needs the physical
device to validate** before it's marketed:
- 📋 **`dfu-util` backend** → Pi Pico / RP2040 (UF2 bootloader also), and DFU-class targets.
- 📋 **`UF2` (mass-storage) backend** → RP2040-family and UF2 bootloaders (drag-drop `.uf2`).
- 📋 **HackRF One** (SDR — flashing + a capture/tx bridge) · **Proxmark3** (RFID) · **Chameleon Ultra**
  (NFC emulation). Each gets a profile + backend adapter + a protocol parser where it has a serial CLI.
- **Approach:** scaffold each backend behind the existing `FlashEngine._backends` registry with unit
  tests over the argv/flow (no board needed), land it on a branch flagged `HW-validation pending`, and
  only flip it to "supported" (README/site) after a real-hardware pass.

### New firmware profiles
The profile model is data-driven (drop a JSON → `profile_loader` resolves it). Track the ESP32/RF
security-firmware ecosystem and add profiles as upstreams stabilize, always SHA-256-pinned for
closed-source binaries and validated on hardware before the count is advertised.

### Orchestration & intelligence (README Phases 2–3)
- 📋 **Mission planner / attack-chain builder** — compose a sequence of target-actions across devices
  (build on `models/mission.py` or replace it), with a dry-run + a per-step confirm for dangerous steps.
- 📋 **Trigger / event system + scheduled tasks** — "when device A discovers X, do Y on B" beyond the
  current single-verb AutoRouter; time/condition-based task engine.
- 📋 **Plugin system** — a stable plugin API so a new firmware/backend/parser is a drop-in package, not a
  core edit (extends the existing data-driven profile + protocol registries).

## Long term — RF & recon (README Phase 4)

- 📋 Signal heatmap · RF waterfall (SDR-fed) · PCAP capture pipeline · Kismet/recon bridge · Meshtastic
  mesh-relay. These lean on the SDR backends above and a capture pipeline; sequenced after the backends
  land and are HW-validated.

## Cross-cutting

- 🚧 **`uf_core` shared engine.** The flash core is being extracted into `universal-flasher` as a
  UI-free installable dependency so there is ONE flash engine instead of two diverging copies; Cyber
  Controller consumes it and keeps its panel + all non-flash features. Ends the two-codebase drift.
- 📋 **On-hardware validation program.** A standing checklist to close the `pending confirmation`
  command sets (ESP32-DIV / HaleHound / Meshtastic-protobuf / S3 / C5 / Pi-SD) and to capture the
  BlueJammer validated control map — the gate for advertising each as supported.
- 📋 **Marketing-surface parity.** cybercontroller.org / esp32marauder.com / lxveace.com / README must
  agree on the canonical counts + versions; a CI check keeps them from drifting.

---

*Reliability-first, honest counts, validate-before-you-claim. This roadmap is a plan, not a promise —
items move only when they're real; each is tracked against the CHANGELOG as it ships.*
