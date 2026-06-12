# Night Session Log — 2026-06-11 → 12 (autonomous)

Detailed running log of the overnight autonomous work. User mandate: do as much as possible,
loop and keep finding work, test all firmwares on connected hardware, deep-research + fact-check
(don't trust existing info), write in-depth context + vision docs, fix issues with the best
solution and push to main via PRs (self-merged), release the UI if it reaches a clean point,
keep detailed logs, experiment freely on all attached hardware. Goodnight given — fully autonomous.

Commit rule everywhere: **LxveAce <extrafadexd@gmail.com>** only, never a Claude co-author.

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

### 5. Meshtastic chip-zip support in CyberC — SHIPPED (PR #2, master `b90de7b`)
Meshtastic moved to per-CHIP zips (firmware-esp32s3-*.zip, 128 MB, every board inside). Rewrote
MeshtasticProfile to a curated board list per chip (13 s3 / 12 esp32, heltec-v3 verified) that
extracts the board's factory bin (flash @0x0); `download_and_extract` now caches/reuses the big
archive. Validated: heltec-v3 extracts the byte-identical 2081488 B bin already booting on COM12.

### 6. Cyberdeck v2 + step-by-step build guide — PUSHED (Projects `ddb25ac`)
6-agent design/audit workflow → `CYBERDECK-V2-ARCHITECTURE.md` (board→role: S2U→BadUSB/
SuperWiFiDuck, T-Display-S3→Flock/OUI-Spy, BW16→5GHz deauth, C5→5GHz backbone) +
`BUILD-GUIDE-STEP-BY-STEP.md` (phased workbench build).

### 7. Security H-1 — SHIPPED (PR #3, master `547036e`)
Audit found the BW16/RTL8720 path flashed a third-party bundle with NO integrity check (the one
path lacking it). Pinned the SHA-256 of the HW-validated bundle + `verify_sha256()` rejects any
mismatch before flashing. Validated end-to-end on the real BW16 (4 files verify OK then flash).

### 8. Unified Action Broadcast — SHIPPED + LIVE-VALIDATED (PR #4, master `ea243f3`)
One verb → every connected radio fires it at once in its NATIVE command (new `src/core/broadcast.py`
engine + `BROADCAST_CAPABILITIES` on all 8 protocols + `broadcast_tab.py` UI wired into main_window;
fixed `_NAME_TO_MODULE` missing bw16; +9 tests, GUI smoke). **LIVE on hardware:** "Find APs" →
COM8 BW16 `AT+SCAN` (39-line dual-band scan) + COM9 GhostESP `scanap` (94 APs), simultaneously.

### 9. Profile asset-matching audit — done
Confirmed my two zip fixes covered the real bugs; the "0-variant" firmwares (flock-you/oui-spy/
sky-spy/airtag/cyt-ng/minigotchi) genuinely have NO GitHub releases (source-only, 404) — correct.

---

## Running task list / vision (squash these)
- [x] GhostESP zip support (PR #1) · Meshtastic chip-zip support (PR #2)
- [x] Firmware×device specialties dossier · Cyberdeck v2 + build guide (Projects)
- [x] Meshtastic flashed + configured (region US, "Cyberdeck/DECK") + working on Heltec V3 (COM12)
- [x] Security H-1: SHA-256-pin the BW16 firmware (PR #3)
- [x] **Unified Action Broadcast** — shipped (PR #4) + live-validated on BW16+GhostESP
- [ ] **Apply remaining security findings** (M-1 subscribe dedup, M-2 vault SSRF allowlist, L-1 NTFS ACLs)
- [ ] **Apply fact-check corrections** (`_smbuild/night_deliverables/fact-check.md`) to repos + push
- [ ] **Apply UI optimization plan** (`_smbuild/night_deliverables/ui-optimization.md`) — keep look+function
- [ ] Sweep remaining flashables on connected boards (Marauder/Bruce/HaleHound re-confirm on current fleet)
- [ ] Raspberry Pi: bring up as cyberdeck core (CyberC on it; drive nodes), talk to ESP-on-Pi-USB, end on Kali
- [ ] Vision-forward doc (squash-all roadmap)
- [ ] **Update websites (cybercontroller.org/esp32marauder.com) — AT THE END**
- [ ] Release the UI if it reaches a clean point (4 PRs in — strong candidate for a tagged release)

## State for continuity
- 4 PRs merged tonight (cyber-controller master `ea243f3`). All as LxveAce. Suite green; GUI smoke passes.
- Fleet: COM3 ESP-AT(stuck) · COM8 BW16-Vampire · COM9 GhostESP · COM10 ESP32-DIV v1.1.0 · COM11 classic-ESP32(SD-fw) · COM12 Meshtastic Heltec-V3. **Pi still NOT present** (scanned repeatedly).
- Deliverable docs to mine: `_smbuild/night_deliverables/{security-audit,fact-check,ui-optimization}.md` (+ .SUMMARY).
- meshtastic CLI + PyQt5 + psutil installed. BW16 AmebaD tool at `_smbuild/bw16/` (CYBERC_AMEBAD_TOOL).

## Open questions (logged, not blocking — simulate/verify)
- Pi connection method (LAN IP / USB-ether gadget / SD here)? Scanning each cycle.
- Tag a v1.x release after the broadcast + zip fixes? (leaning yes — clean, tested, big new feature.)
