# Night Session Log — 2026-06-11 → 12 (autonomous)

Detailed running log of the overnight autonomous work. User mandate: do as much as possible,
loop and keep finding work, test all firmwares on connected hardware, deep-research + fact-check
(don't trust existing info), write in-depth context + vision docs, fix issues with the best
solution and push to main via PRs (self-merged), release the UI if it reaches a clean point,
keep detailed logs, experiment freely on all attached hardware. Goodnight given — fully autonomous.

Commit rule everywhere: **LxveAce <lxveace@proton.me>** only, never a Claude co-author.

---

## Hardware fleet (re-scanned as boards were plugged in)
| Port | Board (detected) | State |
|------|------------------|-------|
| COM3 | ESP-WROOM-32 (CP210x) | ESP-AT, stuck in download mode (needs BOOT tap) |
| COM8 | BW16 / RTL8720DN (CH340) | Vampire Deauther (dual-band, AT+ CLI) |
| COM9 | classic ESP32 (CH340) | **GhostESP** (flashed this session via the new zip path) |
| COM10 | classic ESP32 (CH340K) | (newly plugged — untested) |
| COM11 | classic ESP32 (CP210x) | (newly plugged — untested) |
| COM12 | ESP32-S3 (CP210x) | **Meshtastic Heltec LoRa V3** (flashed + booting this session) |
| (pending) | Raspberry Pi | cyberdeck CORE — full permission to wipe/reflash; end state Kali |
| (pending) | ESP-with-display on the Pi's USB | to be driven FROM the Pi once it's reachable |

Owned but not all connected (from Projects/INVENTORY.md): 3x Lonely Binary ESP32 Gold, 3x ESP32-S2U,
LILYGO T-Display-S3, 2x ESP32-C5, 2x CYD 2.8", AITRIP 4" ST7796, 3x BW16-Kit, Pi 5, Pi Zero 2 W (fried).

---

## Work log (chronological)

### 1. GhostESP zip-bundle support — SHIPPED (PR #1, merged)
- **Bug:** GhostESP ships per-board `.zip` bundles (each with a flashable `merged.bin`), not bare `.bin`,
  so the profile matched nothing on every chip → GhostESP was un-flashable.
- **Fix:** `GhostEspProfile` now accepts `.zip` assets (chip-tagged from the name + a board→chip
  heuristic, `zip_member=merged.bin`); new path-safe `flash_core.download_and_extract()`; `flash_engine`
  extracts then flashes the merged image at 0x0; `default_variant` prefers the chip-generic build.
- **Validated END-TO-END on COM9** (classic ESP32): zip → merged.bin extracted → flashed → hash verified
  → GhostESP booted. 26 esp32 / 10 s3 / 5 c5 variants now discovered (incl. `LilyGo-TDisplayS3-Touch`).
- +6 tests; full suite green. Merged via PR #1 → master `d8a9750`.

### 2. Repo hygiene — stray gitlink removed (master `8a6108a`)
- A broad `git add -A` turned the leftover `suicide-marauder` directory into a gitlink (it was never
  tracked; `.gitmodules` only declares `deadmans-switch`). Untracked + gitignored it. Lesson: stage
  specific paths, not `-A`, in this repo.

### 3. Firmware × Device Specialties dossier — PUSHED (Projects `d2948d2`)
- 16-agent web-research workflow → `Projects/projects/14-cyberdeck/FIRMWARE-DEVICE-SPECIALTIES.md`
  (145 KB): per-firmware specialties, the exact owned-board fit, a quick-pick matrix, and
  verify-on-hardware open questions. Companion to the existing FIRMWARE-REFERENCE.md.

### 4. Meshtastic on the Heltec V3 (COM12) — WORKING
- Meshtastic ships per-CHIP zips (`firmware-esp32s3-*.zip`, 128 MB, all S3 boards inside) — same class
  of issue as GhostESP. Downloaded it, extracted the Heltec-V3 files.
- Flashed the official way (from `device-install.sh` offsets): `firmware-heltec-v3` @0x0 (merged
  factory), `bleota-s3` @0x260000, `littlefs-heltec-v3` @0x300000 — all hash-verified.
- **Boots fully:** LoRa radio up, NimBLE BT (MTU 517), OLED rendering frames, node 1 online. Installing
  the `meshtastic` CLI to query `--info` over serial.

---

## Running task list / vision (squash these)
- [x] GhostESP zip support (PR #1)
- [x] Firmware×device specialties dossier
- [x] Meshtastic flashed + working on Heltec V3
- [ ] **CyberC Meshtastic profile fix** — chip-zip + board-within-zip selection + multi-file install
      (generalize the zip machinery; 128 MB download caveat)
- [ ] **Unified action broadcast** ("one button → every connected radio runs it in its native command")
- [ ] Cyberdeck re-brainstorm with the full real inventory (3x BW16, S2U, T-Display-S3, C5, etc.)
- [ ] In-depth step-by-step hardware build guide (in parts → assembly)
- [ ] Security pass (top-notch) + UI optimization pass (run on all hardware, keep look+function)
- [ ] Fact-check everything in the repos against current upstream
- [ ] Vision-forward doc
- [ ] Raspberry Pi: bring up as the cyberdeck core (CyberC on it; drive nodes), end on Kali
- [ ] Talk to the ESP-with-display from the Pi's USB
- [ ] Test remaining firmwares on COM10/COM11; re-flash boards as needed
- [ ] Release the UI if it reaches a clean point

## Open questions (logged, not blocking — will simulate/verify)
- Which exact board are COM10 / COM11 (both classic esp32)? Will fingerprint by flashing/serial.
- How will the Pi connect (LAN IP / USB-ether gadget / SD card here)? Scanning each cycle.
