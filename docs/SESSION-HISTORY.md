# Cyber Controller / Suicide Marauder — Full Session History

The complete, durable record of this work so any future session resumes with total context. Cross-project
(cyber-controller + Suicide-Marauder + universal-flasher + headless-marauder-gui + esp32marauder.com +
the GitHub profile). Companion to `Suicide-Marauder/docs/NIGHT-SESSION-LOG.md` (the firmware bring-up
detail). Author/commit rule across ALL repos: **LxveAce only, no Claude co-author, single contributor.**

---

## 0. WHO / WHERE / RULES
- Owner **LxveAce** (`extrafadexd@gmail.com`) — Electrical Field Engineer (data centers), security-hardware
  builder, Honda/Acura K-series. Repos under `C:\Users\extra\projects\`. Windows 11, PowerShell + git-bash.
- Python 3.12 at `C:\Users\extra\AppData\Local\Programs\Python\Python312\python.exe` (has pyserial,
  cryptography, esptool 5.3.0, esp-idf-nvs-partition-gen, platformio, pytest). The sandbox python lacks
  these — always use the Python312 path for hardware/crypto work.
- **Tuning/build philosophy:** reliability/longevity over everything; cross-reference how each piece
  interacts with others; LMM thinking + red-team after each phase. Commit/push only when it furthers the
  goal; never Claude co-author.

---

## 1. SESSION 1 (2026-06-10, summarized pre-compaction) — cyber-controller pickup → release
Picked up `LxveAce/cyber-controller` (flagship convergence of headless-marauder-gui + universal-flasher
+ universal-flasher-ui, with Suicide-Marauder as a submodule). Major overhaul on branch
`flash-core-port-and-hardening`, then merged to master + released **v0.3.0 STABLE**:
- **Ported the hardware-validated `flash_core.py`** (15 firmware profiles, esptool plumbing, SSRF host
  allowlist + redirect pinning, path-traversal + TOCTOU SHA-256 bundle verify) and **`profile_loader.py`**
  fixing the **silent-flash bug** (shipped rich JSON profile schema `{id,boards,firmware_urls}` didn't
  intersect the flat dataclass → `files` empty → esptool wrote ZERO binaries).
- Rewrote `flash_engine.py` to delegate to flash_core; real adb/sd/backup/batch backends; recovered the
  bruce/flipper/halehound protocol parsers + registry.
- **Security hardening (15 audit findings)**: SocketIO auth + CSRF + CORS lockdown + rate-limit
  (`web/app.py`, `web_auth.py`), AES-256-GCM fail-closed `encrypted_storage.py` (no default creds; a
  one-time password, no shipped default), control-char rejection in `serial_handler.py`, `_safe_render`
  (no str.format injection) in `cross_comm.py`, SSRF + sha256 + path-traversal in `firmware_vault.py`.
- **Flash offsets by chip (load-bearing):** bootloader 0x1000 (esp32/s2) / 0x0 (s3/c3/c6/h2) / **0x2000
  (c5/p4 — critical, NOT 0x1000)**; partitions 0x8000, boot_app0 0xE000, app 0x10000. esptool argv
  `write_flash -z --flash_size detect --before default_reset --after hard_reset` (anti-brick).
- **Websites** lxveace.com + esp32marauder.com overhauled (SEO tags, promoted the toolchain), and an
  **AI-vibe-coded-website exploitation red-team**研究 logged (docs/RED-TEAM.md + WEBSITE-SECURITY.md) to
  harden the deployed sites.
- **Suicide wipe forensic hardening (red-team round 3, `c701e40`):** overwrite-then-erase (`flash_passes`)
  + raw `esp_flash_read` verify (esp_partition_read decrypts on T2 → false fail), resume-converge fast
  path, GUARDIAN factory + scratch coverage, RAM scrub.
- **CI**: fixed Windows build, re-cut v0.3.0 green (Win/Linux/macOS binaries).
- **HW-validated the flash path** on a real ESP32 (CH340/COM5): scan → detect → 4MB backup → download
  Marauder v1.12.1 → write_flash → hash-verify → boots + responds. v0.3.0 promoted to stable.
- **White-screen issue surfaced:** the engine auto-picked the `old_hardware` Marauder variant for any
  `esp32` (chip-ID can't tell a CYD from a generic board), driving the wrong display driver → blank
  screen. Flashed the `cyd_2432S028` variant as a stopgap; started the password-setup feature.

---

## 2. SESSION 2 (2026-06-10 → 06-11, this conversation)

### 2.1 Suicide-Marauder password & duress SETUP ("Approach A" — host-side, before flash)
- `Suicide-Marauder/host/provision.py`: extracted **`build_bundle(args, pw_buf)`** as the reusable core
  (hashes the password PBKDF2-HMAC-SHA256 **host-side**, zeroizes the buffer, bakes `guardcfg.bin` + the
  bundle manifest; only {salt, pwhash, params} reach the device — never plaintext on disk/argv/logs).
- `cyber-controller/src/core/suicide_setup.py` (`SuicideConfig` + `build()`/`run_cli()`) wraps it; surfaced
  as **`cyber-controller --suicide-setup`** (interactive getpass CLI, wired in `app.py`) and the Qt
  **Tools ▸ Suicide Marauder Setup** dialog (`src/ui/qt/suicide_dialog.py`). Tested: guardcfg minted,
  password zeroized.

### 2.2 White-screen ROOT CAUSE fixed (variant selection)
- A CYD, M5Stick, and bare dev board all detect as `esp32`; the per-chip default `old_hardware` silently
  flashed the wrong display driver. Fix: `FirmwareProfile.variant` + `FlashEngine._resolve_variant()`
  (always LOGS the chosen variant + a "set a variant if your display stays blank" hint) +
  `list_variants()` + a Qt FlashTab "Board / variant" picker (loads off-thread; ignores stale results).
  Owner confirmed the **CYD screen is fully functional**.

### 2.3 Host↔firmware PARITY AUDIT (22-agent workflow) — "make sure the suicide part works"
The make-or-break question: does the host-hashed password match what the firmware re-derives at boot?
A workflow mapped every subsystem and adversarially hunted divergences; I independently built an
executable parity proof (`Suicide-Marauder/.git/test_parity.py`). Verdict: **the contract is sound and
there is NO fail-open in the wipe** (armed=0/unprovisioned can never wipe). Confirmed defects fixed:
- **CRITICAL — guardcfg too small:** the 4MB CSV carved an 8 KB (`0x2000`) `guardcfg`, but ESP-IDF's
  **read/write NVS minimum is `0x3000`** (3 sectors). `nvs_partition_gen` silently emitted a 4096-byte
  READ-ONLY image; on-device `nvs_flash_init_partition("guardcfg")` (RW, the gate stores its sgate_rt
  counter there) would FAIL → `provisioned=false` → **the password gate would never activate.** Fixed →
  `0x3000` (spiffs trimmed 0x1000; coredump/scratch offsets unchanged) + SPEC §3.1; `provision.py` now
  hard-rejects guardcfg < 0x3000. Only the 4MB build was affected (8/16MB already ≥0x4000).
- **Owner-safety password guards (`validate_password`)** — reject anything the firmware would hash
  differently (→ silent lockout / armed-board self-wipe): **>63 UTF-8 bytes** (firmware char[64] clamps),
  **leading/trailing whitespace** or a **`unlock ` prefix** (serial adapter strips them), and
  **`--kdf-iter > 0xFFFFFFFF`** (stored as NVS u32). Enforced in CLI + the programmatic build_bundle path.
- **CYD touch build define gap:** `GATE_INPUT_TOUCH` `#error`s without `-DSUICIDE_HAVE_TOUCH_KEYBOARD_OBJ`;
  added it to the FORK touch path in build.ps1/.sh, platformio.ini.example, INTEGRATION.md.
- **Doc fixes:** re-provisioning DOES reset the attempt counter (whole-partition guardcfg reflash erases
  sgate_rt) — corrected SPEC + PROVISIONING; fixed the stale `sgate_rt` CSV row + 0x2000 size; added the
  template's missing flash_passes/fast_wipe rows.
- Propagated to canonical + both vendored copies + cyber-controller submodule.

### 2.4 Built the CYD touch Marauder FORK from source + on-device gate test
- No Marauder source/toolchain existed → set up `_smbuild/`: arduino-cli 1.5.1, esp32 core **2.0.11**,
  16 pinned libs (TFT_eSPI V2.5.34 with `User_Setup_cyd_micro`, NimBLE 1.3.8, etc.) — exact refs from
  Marauder's CI `build_parallel.yml`; `-zmuldefs` platform.txt patch; cloned ESP32Marauder (v1.12.2).
- Injected the gate into `esp32_marauder/`: bootgate flat-copied, `.ino` patched (includes + the
  fail-closed `if (suicide::BootGate::run()!=GATE_PASS) esp_restart();` before `settings_obj.begin()`),
  `partitions.csv`=suicide_4MB. FQBN `esp32:esp32:d32:PartitionScheme=min_spiffs`. Caught + fixed: the
  touch shim (`keyboardInput` is a FREE function, not a `touch_keyboard_obj` method) and non-ASCII chars
  in the partition CSVs (the core's gen_esp32part.py reads ASCII → crashed → broke every build).
- Owner confirmed the **on-screen keypad / unlock / wrong-error all work** on the CYD.

### 2.5 ⭐ FORENSIC OBLITERATION BRICK — hardware-validated
Owner clarified the wipe must **obliterate the firmware itself** (Marauder gone, no boot), wipe SD where
present, and **overwrite the same flash regions** so it's forensically unrecoverable. The SAFE_MODE build
only simulates; the live brick was the unverified primitive. Debugging journey (full detail in the night
log), boards COM5 (CYD) + COM7 (blank ESP32):
- `wipeInternal` (data partitions) already worked via esp_partition. The **running-app + boot-chain
  self-erase** was the hard part: on the stock arduino core (`CONFIG_SPI_FLASH_DANGEROUS_WRITE_ABORTS=y`),
  `esp_flash_erase_region` `abort()`s on BOTH the protected boot chain AND the running app slot.
- Fix (in `SelfDestruct.cpp brickBootChain`, ESP32 path): erase the running app FIRST (gone even if a
  later step fails), via the **ROM SPI driver** (`esp_rom_spiflash_unlock/erase_sector/write`) inside the
  IDF flash-only critical section **`spi_flash_disable_interrupts_caches_and_other_cpu()`** (declared
  `extern` — not in a public header but exported by libspi_flash.a; it disables IRQs + stalls the other
  core + disables the cache the *correct* idle-then-clear way — a manual `Cache_Read_Disable` wedged the
  SPI0/SPI1 arbitration, found via direct-UART `brickMark` markers since arduino suppresses ESP_LOG).
  Forensic random-overwrite pass + final erase on the app, then the partition table + 2nd-stage
  bootloader, then RTC_CNTL `SW_SYS_RST` (esp_restart lives in the erased app). Disable RTC + **TG0/TG1**
  watchdogs first (saw TG0WDT_SYS_RESET cutting the multi-second erase short).
- **RESULT (verified by esptool read-back, all 0xFF):** bootloader@0x1000, partition table@0x8000, full
  app 0x10000..0x1F0000, NVS/SPIFFS/coredump, guardcfg — the **entire flash obliterated**. Board
  boot-loops in the indestructible mask ROM ("invalid header: 0xffffffff"); recoverable only by the owner
  over UART (T1, no eFuse). Validated **twice** on the CYD (dead-man trigger).
- Also fixed the **SD no-card abort** (`sdmmc_card_init` crashed on SPI-SD/no-card boards): the raw SDMMC
  path is now opt-in `-DSUICIDE_SD_SDMMC`; default is the abort-safe `SD.begin()` file-level path.

### 2.6 ⭐ UNIVERSAL (firmware-agnostic) DEAD-MAN SWITCH — built + validated
- `Suicide-Marauder/firmware/guardian/guardian.ino` (+ README): the gate **standalone, NO Marauder**
  (builds 349 KB, no TFT/NimBLE) — proves it's firmware-agnostic (GUARDIAN model: gate in `factory`, any
  firmware in `ota_0`; on PASS `esp_ota_set_boot_partition(ota_0)` + reboot).
- **Hardware-validated on the blank ESP32 (COM7)** via a **wrong-password ×2 serial trigger** (the owner's
  core "2 fails → wipe" spec) → full obliteration (all 0xFF). So the wipe is now proven on **2 boards,
  2 trigger paths (dead-man + attempts), serial + touch input**.
- The "no boot attack bypasses the password" guarantee = the GUARDIAN anti-skip (an attacker who can write
  flash can rewrite `otadata` to boot ota_0 and skip the gate) → closed only by **T2** (Secure Boot v2 +
  APP_ROLLBACK + gate re-asserts factory). IRREVERSIBLE — owner choice C2.

### 2.7 Dashboard (cyber-controller) logic validated
- Full pytest suite **107/107 PASS** — cross_comm (EventBus/TargetPool/AutoRouter = the cross-device
  "one board's AP, another executes on it" routing), encrypted_storage, flash_core, profile_loader,
  protocols, serial_handler, web_auth. Fixed the suite to run with a bare `pytest` (added
  `[tool.pytest.ini_options]` pythonpath/testpaths — it was failing to import `conftest`).
- **Live cross-comm route→deliver HARDWARE-VALIDATED** (2026-06-11): new `tests/test_cross_comm_live.py`
  (skips without `CC_LIVE_PORT`) — wires the REAL DeviceManager + EventBus + AutoRouter, opens a real
  board (COM7), injects a "device A discovered an AP" event, and the AutoRouter matched a rule and
  **delivered the routed command over the real serial connection to device B**. So the cross-device
  route→deliver chain (the "one device's AP, another executes on it" core) is proven on real hardware,
  not just the unit logic. Suite now 107 passed + 1 skipped (108 with the live port).
- STILL next-session: the FULL "device A's live Marauder scan auto-routes to device B's Marauder execution"
  end-to-end needs (a) a no-display Marauder build for the bare ESP32 + (b) a Marauder serial→target.added
  parser feeding the bus; UI-runtime smoke (PyQt5); optional install-password default (C3); device-side
  anti-boot-bypass = T2/eFuse (C2). RESOLVED open item: `SerialConnection.write` correctly appends
  exactly one `\n` terminator (and rejects only EMBEDDED control chars as an injection guard), so routed
  commands DO terminate + execute on the device — the cross-comm command delivery is fully functional.

### 2.8 Docs / READMEs / website / profile
- **Profile README** (`LxveAce/LxveAce`): added Cyber Controller as the flagship; updated Suicide
  Marauder to the HW-validated obliteration.
- **cyber-controller README**: `--suicide-setup` + the validated obliteration.
- **Suicide-Marauder README**: Status "Brick primitive" → HARDWARE-VALIDATED (was UNVERIFIED).
- **esp32marauder.com**: promoted the validated full-flash obliteration + SEO keywords.
- CYD **recovered** to a working touchscreen Marauder.

---

## 3. HARDWARE FLEET (connected over the session)
| Port | Board | Chip / flash | Download mode | Notes |
|------|-------|--------------|---------------|-------|
| COM5 | CYD 2432S028 (2.8" touch) | ESP32 4MB | OK (CH340) | wipe HW-validated ×2; recovered to Marauder |
| COM7 | blank/erased ESP32 dev | ESP32 (CH340K) | OK | universal-gate wipe HW-validated (wrong-pw); free test board |
| COM8 | AITRIP 4.0" touch (ST7796 320x480) | ESP32 8MB (GD c4/6016) | OK (CH340) | NEW Marauder board to add (ST7796 not stock) — needs pinout (C5) |
| COM3 | ESP32-WROOM, ESP-AT v2.4.0 | ESP32 (CP210x) | **BLOCKED (0x13)** | no auto-program circuit → needs a BOOT-button tap (C1) |
| —    | Pi Zero 2 W (pwnagotchi) + Waveshare 2.13" V4 e-ink + PiSugar S | ARM Linux | n/a | SD-image + SSH platform, NOT esptool; display=`waveshare_v4` (jayofelony fork). Only exposed a driverless USB-serial gadget (C4) |

---

## 4. REPO STATE (end of this block — all clean + pushed, LxveAce only)
| Repo | HEAD | What |
|------|------|------|
| Suicide-Marauder | `3ccb2b9` | forensic brick + universal guardian gate + parity/SD/touch/CSV fixes + NIGHT-SESSION-LOG |
| cyber-controller | `bbbddee` | --suicide-setup, board/variant picker, submodule bump, pytest config (107 pass), this doc next |
| universal-flasher | `fd6ad03` | vendored brick + fixes |
| headless-marauder-gui | `8cb2906` | vendored brick + fixes |
| esp32marauder.com | `56ea84f` | obliteration promo + SEO |
| LxveAce (profile) | `d3ca720` | Cyber Controller flagship + Suicide update |

---

## 5. OWNER CHOICES (decide next session — nothing is blocked waiting on these)
- **C1 — COM3 (ESP-AT WROOM):** needs a one-time BOOT-button tap (hold BOOT, tap EN/RST, release) to enter
  download mode — no auto-program circuit. Then it's flashable + testable.
- **C2 — T2 / eFuse tier (most important):** tonight's wipe is **T1 (owner-reflashable over UART)**. A
  truly unrecoverable wipe AND the "no boot attack can bypass the password" guarantee BOTH require
  **IRREVERSIBLE** Secure Boot v2 + Flash Encryption + UART-download-disable eFuses. Confirm before any burn.
- **C3 — dashboard install password:** default on or off, + reset path.
- **C4 — Pwnagotchi:** reflash its SD with the jayofelony image set to `ui.display.type="waveshare_v4"` +
  a USB-**ethernet** gadget so the controller can SSH-debug the 2.13" V4 e-ink (it only exposes a
  driverless serial gadget now). Or hand over the SD in a reader.
- **C5 — 4" ST7796 board (COM8):** not a stock Marauder target. Adding it needs that board's exact
  TFT_eSPI display+touch pinout (ST7796 320x480) — share the product/wiki link or I best-guess from the
  AITRIP/Sunton reference, then add a `User_Setup` + a `MARAUDER_*` board define.

---

## 6. NEXT-SESSION QUEUE (unblocked work to keep going)
1. Test the wipe on COM8 (8MB / 4" board) via the guardian (display-agnostic) — adds the 8MB layout.
2. Tidy the cosmetic `locked for 0s` message in the armed wipe path (wipe still fires; ordering only).
3. Per-chip ROM brick for S2/S3/C3/C6 (register addresses + `esp32sX/rom/...`) — none attached yet.
4. Dashboard: wire the install-password first-run flow (per C3), then a scripted 2-board cross-comm
   integration test (board A Evil Portal AP ↔ board B station scan feeds A's target list), then UI smoke
   tests (PyQt5/Tk/TUI/Web).
5. Add the 4" ST7796 board profile once the pinout is known (C5); add COM3 once tapped into DL mode (C1).
6. Pwnagotchi display debug once it's reachable as an ethernet gadget (C4).
7. Build the full GUARDIAN factory+ota_0 bundle (universal gate protecting a real firmware end-to-end).
