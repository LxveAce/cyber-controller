# Changelog

All notable changes to Cyber Controller are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [1.9.0-beta] — 2026-07-21
A broad feature beta. Backend/serial control only — CC issues firmware CLI commands
and never authors radio frames.
- **New: the bottom terminal echoes everything, and you choose where a line goes.** Every command and every
  device return now surfaces in the always-visible terminal — the typed commands, the Operate console, the
  routed/AutoRouter sends, and a Devices-tab board's replies — not just serial RX. A send-target selector
  (Auto / Device(s) / Computer) lets you pick whether a typed line runs on the computer's tool shell or goes
  to the connected board.
- **New: the Flock map is a real street map.** A real OpenStreetMap street basemap now sits under the camera
  markers instead of a country outline that showed nothing at city zoom. It's built natively (no heavy web
  engine) and works offline — cached tiles render with no network; turn on "Online tiles" once with internet
  to cache your area. The old "work in progress" banner is gone. Online tiles are OFF by default (airgapped).
- **New: wardrive upload to WiGLE and WDG Wars.** From the Wardrive tab, upload your WigleWifi CSV straight to
  your WiGLE or WDG Wars (wdgwars.pl) account — pick the service, set its token in Settings, hit Upload. It
  runs off-thread and leaves the file on disk to retry; the token is validated so it can't leak into a log.
- **New: Terms of Service & Use in Help.** A proper terms dialog (Help ▸ Terms) framing the tool for
  authorized research/lab use, authorized targets only, with the interference/§333 posture and no-warranty /
  liability clauses. It does not assert a specific certification the app can't back up.
- **Changed: the Operate surface is regrouped.** The crowded 8-tab Operate is now the live action loop
  (Targets · Broadcast · Console · Macros); a new **Survey** surface groups the GPS field tools (Wardrive ·
  Multi-Wardrive · Flock Map); and the old Network surface is now **Analyze** (Graph · Cross-Comm · Crack Lab ·
  BLE Analyzer).
- **Changed: much fuller, honest per-firmware command control.** Added the real, documented CLI verbs each
  firmware actually supports and removed the ones that were fake. GhostESP grew from 32 to ~92 verbs
  (connect/recon suite, capture variants, evil-portal management, AirTag/Flipper tooling); Bruce gained its
  real serial-shell verbs; ESP-AT grew to the real Espressif AT set; Marauder gained the Evil Portal group;
  Flipper gained loader control; BlueStress's four dead placeholder commands were replaced with its 15 real
  ones. Firmwares with no scriptable CLI (HaleHound, Meshtastic) no longer present fake command buttons —
  they're flash-and-monitor only. Every offensive verb is danger-flagged so the safety gate is authoritative.
- **New: one canonical captures folder.** Retrieved `.pcap` files, auto-EAPOL `.hc22000`, and Crack Lab's
  Browse now all point at `~/.cyber-controller/captures`, so a just-captured file is one click from the cracker.
- **New: the Settings ▸ Updates card shows the update status.** It reads the running version, when it last
  checked, and whether a newer release is out — so you can see the auto-update check ran instead of guessing.
  The stale "no self-update" copy is corrected to describe the real in-place update.
- **Fixed: the network graph's zoom and panning.** The Graph view no longer zooms into the void (its wheel
  zoom is clamped), and you can drag empty background to pan the canvas while still dragging individual nodes.
- **Fixed: several background-thread and reliability issues from an internal review** — the map-tile fetcher
  and the WiGLE/WDG upload are joined cleanly on close (no crash-on-exit), the street basemap no longer blanks
  on a large 4K/ultrawide monitor, and an upload token with a stray line break is rejected instead of leaking.
- **New: the Auto-detect firmware selector now shows what it detected.** After a board is identified on
  connect, the Devices-tab firmware picker reads "Auto-detect (detected: ESP32 Marauder)" instead of a bare
  "Auto-detect" that scrolled the result off in the terminal. It only claims a detection when there's a real
  identifying banner (never the connect-time default guess), clears when the device disconnects, and the
  auto-vs-forced logic is now index-based so the dynamic label can't desync it.
- **Changed: the Operate console now works on every firmware, not just LxveOS.** Offensive commands
  (deauth, beacon, spam, jam, and the like) used to be permanently blocked on Marauder, ESP32-DIV, GhostESP,
  and Bruce, because the "armed" state they required is only implemented by LxveOS. Those firmwares have no
  arm handshake, so the console now confirm-gates each offensive command (type-to-confirm) instead of
  dead-ending it. It also hides the unusable two-factor arm box on those firmwares, names the command grid
  after the connected firmware, and colors dangerous buttons (amber for lab-only, red for jamming) so they
  read differently from a scan at a glance. LxveOS keeps its two-factor arm lockout. For authorized lab use.
- **New: read-only "Detect chip" — no more "unknown chip" for a classic ESP32.** A "Detect chip" button reads
  the real chip over serial (esptool `chip_id`, no firmware overwrite) and shows it ("Detected: esp32"). CC caches
  the confirmed chip and prefers it over the USB-VID guess for the firmware-compatibility hints, so a classic
  ESP32 on a shared CP210x/CH340 bridge is no longer treated as an unknown chip. First step of the auto-detect
  hardening; validated on real ESP32 hardware.
- **Fixed: LxveOS is auto-detected on connect.** The connect-time probe also sends `status` (LxveOS answers with
  its `LXVEOS/1` identity line, which classic Marauder's `help` reply doesn't reveal), and the firmware-signature
  matcher now recognizes LxveOS, so an LxveOS board is identified and routed to its own parser instead of staying
  on the provisional Marauder default.
- **New: a BLE Analyzer view.** A firmware-agnostic Bluetooth analyzer that reproduces the on-device
  view — a live RSSI-over-time graph plus a de-duplicated device table — so one view serves every BLE
  firmware (Marauder / GhostESP / Flipper / HaleHound / ESP32-DIV / LxveOS). Built on a Qt-free,
  unit-tested `BleAnalyzerModel` and fed by a guarded event tap on the ingestor's parsed-event stream,
  so it also sees LxveOS `addr`-keyed adverts (with tracker/company) that the mac-keyed target pool drops.
  Awareness-only; it transmits nothing.
- **New: +8 LxveOS passive detectors in the CC command catalog.** Synced the CC bridge with LxveOS
  Phase-2's new CLI verbs — `blewardrive`, `pwnwatch`, `flipper`, `meta`, `skimmer`, `flock`, `surveil`,
  and `watch` — so the palette, `features` surface, and Operate command grid mirror the real device. All
  eight are passive detectors (no emitter), marked danger-free.
- **Fixed: LxveOS BLE adverts now reach the target pool / AutoRouter.** The `ble_found` routing branch
  read only `mac`; LxveOS keys its BLE address as `addr`, so those detections never became pool targets.
  It now accepts `mac` or `addr`, matching the analyzer's firmware-agnostic key handling.
- **Fixed: 'Detect board' no longer switches you off a firmware that already supports the panel.** Running
  CYD detection while a display-capable profile is selected (e.g. LxveOS, which supports the 3.5" and 2.8"
  CYDs) now keeps that profile and points you at the matching board, instead of silently jumping to Marauder
  and dropping your panel choice. Detection still steers to Marauder from a profile that can't flash a display.
- **Fixed: the firmware download cache is now keyed on the URL, not just the asset name.** Two releases that
  ship an identically-named asset (a new version of the same firmware, or a different firmware's `firmware.bin`)
  no longer collide in the cache — a mismatch re-downloads instead of flashing the first one's stale bytes.
- **Fixed: the offline vault caches the right image.** It now honors a profile's asset-match excludes when
  choosing which build to store, so it can't cache an app-only/OTA image that the offline flash would then
  write at 0x0 without a boot chain.
- **Fixed: refreshing the port list keeps your selected port.** Clicking Refresh (e.g. after plugging in another
  board) no longer silently reselects the first device, which could point the next Flash at the wrong board.

## [1.8.0] — 2026-07-15
Feature release: first-class support for **LxveOS** (the LxveAce security-panel firmware), a new
**Operate console** for single-device control, a passive **network-intel** pass on the Targets surface,
and a broad security + reliability hardening sweep. Backend/serial control only — CC issues firmware CLI
commands and never authors radio frames.
- **New: LxveOS is a first-class firmware.** Recognize, flash (from the rolling `ci-latest` channel, a
  merged single image at `0x0`), and control LxveOS boards end-to-end. CC speaks the `LXVEOS/1` serial
  protocol — status / info / caps parsing, the full command catalog behind an `agree` ACK-gate and a
  two-factor `arm` state, live device identity + runtime-capability chips decoded from the caps bitmask,
  and an `airspace` occupancy snapshot surfaced as a Devices-tab tile. Flash + boot + status framing
  validated on real ESP32 silicon.
- **New: an Operate console.** A single-device, button-driven control surface with a telemetry header, a
  prominent SAFE/ARMED lamp, a two-factor arm toggle, and a per-firmware, category-grouped command grid.
  Offensive transmit verbs stay disabled until the device is actually ARMED (the TX-lockout invariant);
  it's a read-only, poll-driven view that opens no serial connection of its own.
- **New: passive network intel on Targets.** Scanned devices are labelled by manufacturer (OUI → vendor),
  a passive channel-occupancy survey shows how busy each channel is, and a target-freshness summary flags
  how recently each was seen — all passive (no probes), exposed on the web remote too (`/api/channels`,
  `/api/freshness`).
- **New: firmware-variant (board) picker on the web flash page.** Pick the exact board build; the chosen
  variant is forwarded to the flash, so a board whose default asset targets a larger flash is no longer
  forced onto the non-booting default.
- **Improved: CYD board-detection actually applies its result.** A "Detect board" result now pre-selects
  the detected panel variant even when Marauder is already the chosen profile — previously the pick was
  silently dropped and Flash wrote the generic ILI9341 default over the panel just identified.
- **Improved: the Crack Lab's JSON export is reachable.** The capture-log export offers CSV *or* JSON
  (the JSON writer existed and was tested, but only CSV was wired up).
- **Security:** `/api/flash` is now per-IP rate-limited like the other command actions; the session cookie
  is forced Secure behind an upstream TLS proxy (`CC_WEB_COOKIE_SECURE`); a non-dict JSON body can no
  longer 500 an endpoint; serial-parser regexes are bounded against ReDoS; the firmware-catalog signature
  check gates on a *good* (non-revoked / non-expired) GPG signature; two AES-GCM nonce-reuse paths in
  per-node provisioning are closed; a truncated download is rejected instead of trusted; a non-ASCII
  CSRF token fails closed to a clean 403 instead of 500-ing the endpoint; and the offline firmware
  vault honors a tag pinned in a profile's URL, so a rolling-prerelease firmware (LxveOS `ci-latest`)
  is cached from the release it points at rather than silently resolving to `/releases/latest`.
- **Fixed — capture / crack accuracy:** an AVS (link-type 163) capture is parsed instead of read as empty;
  a 4-way handshake Message 4 is no longer misclassified as Message 2; wordlists split on any CR/LF and
  reuse the PMK per ESSID salt; a fabricated aircrack ESSID result and MAC-less "phantom" OUI resolutions
  are eliminated; offensive macros are arm-gated via the safety classifier, not a fixed prefix list.
- **Fixed — parsers & UI:** Marauder / GhostESP parser corrections (a crafted BLE name can't be misrouted
  to an AP, BSSIDs are case-canonicalised, multi-line scan output parses); BlueJammer control is serialised
  through one FIFO worker and can't outlive shutdown; port scans and heavy table rebuilds are moved off /
  coalesced on the GUI thread; Connect/Disconnect and Targets-Clear give honest feedback; an honest
  "source-only" message when a firmware ships no flashable binary.

## [1.7.2] — 2026-07-12
Feature release: the Crack Lab now keeps a live, exportable log of every WPA handshake / PMKID your
devices capture, and ties a targeted deauth to the handshake it produces. Backend correlation only —
CC issues firmware CLI commands and never authors radio frames.
- **New: an auto-populating "Captured handshakes" list in the Crack Lab.** Every handshake / PMKID a
  connected device reports appears the instant it's captured, carrying all its metadata (SSID, BSSID,
  channel, client MAC, EAPOL vs PMKID, RSSI, source device + firmware, and the on-device `.pcap`/`.hc22000`
  path). Double-click a row to load it straight into the cracker; when a crack succeeds, the recovered key
  is written back onto that record and the row turns green.
- **New: export the capture log to CSV or JSON.** One button writes the whole log to a spreadsheet-safe CSV
  (every attacker-influenced field is neutralised against CSV-injection) or JSON. Recovered passwords are
  included (the crack flow is consent-gated) — the dialog says so.
- **New: smarter deauth capture-confirm.** Firing a targeted Deauth AP now arms a short window; if a
  handshake for that AP is captured inside it, CC logs a first-class "handshake captured — deauth confirmed"
  line (and an honest "no handshake within the window" if none arrives). Non-deauth capture actions are
  labelled accurately, never as a deauth.
- **Reliability:** a later unrelated capture file can no longer overwrite an earlier handshake's saved-file
  reference; a single handshake that also writes a `.pcap` is counted once, not twice; a recovered key can
  no longer be mis-attributed to a capture you're no longer cracking; timeout notices fire with correct
  timing. (Found and fixed via an adversarial red-team of the new pipeline.)

## [1.7.1] — 2026-07-12
Patch release: two fixes for regressions found during live 1.7.0 QA. No feature changes.
- **Fixed: the Devices-tab Connect/Disconnect buttons did nothing.** The buttons act on the selected device,
  but the list never auto-selected a row after a scan — so the active port stayed empty and clicking
  Connect/Disconnect silently no-opped. The list now auto-selects the first device when nothing is active,
  so the buttons work immediately after a scan (a device you've explicitly picked is still respected).
- **Fixed: the Network graph went stale after reflashing a board.** The graph only refreshed on target
  (scan-result) events and never on device changes, so a reflash (disconnect → reconnect, often re-detected
  with new firmware) left it frozen. It now also refreshes when the connected-device set changes, so it
  self-heals across a reflash — while still preserving any layout you dragged.

## [1.7.0] — 2026-07-11
- **Renamed the "Wi-Fi Audit" tab to "Crack Lab."** The tab is the full offline handshake→convert→crack
  pipeline (capture → `hcxpcapngtool` → `hashcat`/`aircrack-ng` + wordlists), so the name now reflects what it
  does. Same consent-gated, dictionary-only behaviour; only the label + code symbols (`crack_lab_tab.py` /
  `CrackLabTab`) changed, and the tab gained an icon (an open padlock).
- **Crack Lab now has a BUILT-IN native WPA/WPA2 cracker — works out of the box, no external tools.** WPA-PSK
  dictionary cracking is standard crypto, and Python's `hashlib.pbkdf2_hmac` is C/OpenSSL, so CC cracks
  natively (`src/core/native_crack.py` + `wpa_capture.py` — CC's own PMKID/handshake extractor). Validated
  against the canonical IEEE 802.11i PMK vectors + hashcat's own mode-22000 example. It is the default engine,
  needs no install, and there is nothing for antivirus to flag; hashcat/aircrack-ng are optional accelerators
  (detected if installed, or an opt-in bundled aircrack pack behind a transparent, consented Windows Defender
  exclusion). Dictionary-only, consent-gated, verify-never-fake.
- **The bottom terminal is now a unified activity console + a tool shell.** It reflects everything going on —
  flashing (incl. every esptool line), command execution, broadcasts, crack runs, macro playback — via a new
  `ActivityLog` bus, not just serial RX. It also runs the crack tools with their FULL command line: type
  `aircrack-ng …` / `hashcat …` and it runs the bundled/installed tool, streaming output (scoped to known
  tools; `stop` kills it). The serial path + anti-forgery html-escaping are unchanged.
- **A small offline WPA wordlist core is bundled** (SecLists MIT subset, hash-verified) so a first crack works
  with zero download; larger lists still install on demand.
- **Operate ▸ Broadcast — per-device sections + force-any-firmware.** Keeps the universal fan-out row and adds
  a section per connected device: a force-to-any-firmware picker (exposing that firmware's command set even if
  it may not run on the hardware — full manual control), capability chips, and per-device buttons. Everything
  populates reactively (new `DeviceManager.set_firmware` + `on_device_changed` event; the Devices tab and
  Broadcast now stay in sync, and a force survives re-autodetect). Dropped the dead `MESH_RELAY` phantom button.
- **Flock Map: a "Work in progress" banner + a launch-render fix.** The map (and the Network graph) framed
  against a not-yet-sized viewport at construction and opened at the wrong scale until a manual resize; they
  now re-fit correctly on first paint.
- **Resizeable-tab walkthrough + a whole-app performance pass.** Non-scrolling tabs are wrapped in scroll areas
  so controls never clip on a small/deck window; the 8 per-tab refresh timers now run only while their tab is
  visible (a backgrounded tab costs ~0), and the Network graph defers its rebuild while hidden.
- **Reskin: `colors.py` is now the single palette source of truth** (the QSS uses `${TOKEN}` placeholders it
  substitutes), and the off-brand acid-green (`#39FF14`) splash is fixed to the LxveAce violet accent.
- **"Check for Updates" in the Firmware Vault.** The vault could cache firmware and flash offline, but
  `FirmwareVault.check_updates()` (one GitHub API call per *cached* profile, SSRF-allowlisted) was
  CLI-only — no way to ask "is any of my cached firmware outdated?" from the app. Added a **Check for
  Updates** button to the Flash tab's Firmware Vault card: runs the check off the UI thread and reports
  each outdated profile ("cached X → latest Y") or "all cached firmware is up to date" in the flash log.
  Read-only — reports what's stale, downloads nothing.
- **Flash tab now routes a picked Meshtastic UF2 board to the uf2 backend.** The 1.7.0 Meshtastic
  UF2 family (nRF52840 / RP2040 / RP2350) added the resolver + engine dispatch, but the Flash tab only
  set `profile.variant` — not `profile.chip` — so a picked RAK4631/Pico2 kept `chip=auto` and fell
  through to esptool (which can't write a `.uf2`). The tab now recovers the picked variant's chip and
  sets it when the variant is a UF2-family board, so the engine's `_uf2_family_backend` routes it
  correctly; esptool variants keep chip auto-detect untouched. (Physical drag-drop still HW-gated.)

### Not everything in 1.7 is 100% yet — and that's on purpose
The core is solid and the suite is green (2107 passing), so 1.7.0 ships now. A few things are still being sharpened and
land in follow-up releases — we'd rather ship the working core and keep rolling out improvements than sit on it:
- **The Flock offline map** rides along behind a "work in progress" banner. The scanning → Targets → network-graph flow
  is done; the map's polish is what's next.
- **Native cracking covers WPA/WPA2-PSK.** WPA3/SAE (and the AES-CMAC handshakes) route to hashcat for now — native
  support arrives once the test vectors are locked down. Nothing is silently mis-verified: an unsupported handshake says so.
- **Stopping a bundled external tool** (aircrack-ng / hashcat) ends the run but can let the tool finish its current chunk;
  the native engine — the default — stops instantly. Tightening the external-tool stop is on the list.
- **The one-click bundled-aircrack install** is checksum-verified but hasn't had a fresh-machine end-to-end pass yet. You
  never need it — native cracking is built in — and that path gets its real-machine pass next.

More lands as it's ready.

## [1.7.0-beta] — 2026-07-09

_Beta. Multi-firmware release: the offline Wi-Fi crack pipeline + wordlist manager, the BlueStress and ESP-AT
firmware integrations (44 profiles), the nRF BlueNullifier 2 study target, auto-detect per-firmware parser
routing, and a full firmware-integration audit + honesty pass._

### Changed
- **Auto-detect now routes every firmware to its own parser, not always Marauder (1.7.0 multi-firmware).**
  With the firmware selector on "Auto-detect", the Devices tab used to parse every non-Flipper board with the
  Marauder grammar — so a GhostESP / Bruce / HaleHound / ESP32-DIV / BW16 / Meshtastic device ran through the
  wrong parser and wouldn't populate the target pool or expose its own command set. Two parts: (1) auto-detect
  now prefers a real detected firmware (from the device's detected id or its probe banner) over the Marauder
  default; (2) on a never-probed board — where the parser is chosen at connect, before the reply comes back —
  the connect-time handshake now **re-detects and hot-swaps the cross-comm ingest parser** to the firmware it
  actually found, so scans start populating on the right firmware without the user pre-probing or picking
  manually. An explicit (non-Auto) firmware choice is always honoured and never overridden.

### Added
- **Sniffle BLE 4.x/5.x sniffer + the new `cc2538_bsl` flash backend (45 profiles).** New `cc2538_bsl`
  backend wraps `cc2538-bsl` to flash TI CC13xx/CC26xx parts over their ROM serial bootloader (prefer the
  sultanqasim fork for the CC2652P reset fix) — the first backend that can drive a TI dongle, and it unlocks
  the whole CC2652P / Z-Stack / CatSniffer ecosystem. First consumer: `sniffle` (nccgroup/Sniffle, GPL-3.0),
  a BLE link-layer sniffer (follow connections, decode extended advertising, relay to Wireshark) on the SONOFF
  CC2652P USB Dongle Plus (+ CatSniffer V3). Intel-HEX (self-addressed), so app-offset/baud stay `verify:`;
  `danger="lab-only"` (active scan + connection-follow + link-layer relay/MITM — labelled, never blocked). The
  backend is argv/flow unit-tested (golden argv locked); real-hardware validation of the BSL flow is pending.
- **Z-Stack Coordinator + CatSniffer V3 profiles (47 profiles) — the cc2538_bsl backend's 2nd/3rd consumers.**
  `zstack_coordinator` (Koenkk/Z-Stack-firmware — Zigbee 3.x coordinator/router for CC2652/CC1352 dongles like
  the Sonoff ZBDongle-E; `danger=""`, a legit-protocol radio; per-board `.zip` wraps one `.hex`) and `catsniffer`
  (ElectronicCats CatSniffer V3 — passive 802.15.4/Zigbee/Thread/BLE/sub-GHz sniffer; CC1352P7 radio via
  `cc2538_bsl` + RP2040 bridge via `uf2`; `danger=""`). CatSniffer's `asset_match` is scoped to the sniffer build
  and `exclude_regex` drops the bundled airtag_spoofer (active TX). Offsets/SHAs `verify:` until real-hardware.
- **Meshtastic coverage expansion — ESP32 board-list refresh + nRF52840/RP2040/RP2350 UF2 support.**
  Item A (esptool, existing backend): pruned four dead ESP32 slugs (`tbeam0_7`, `heltec-v1`, `heltec-v2_0`,
  `heltec-v2_1` — verified absent from the 2.7.26 manifest, so they were advertised-but-unflashable),
  refreshed the stale `esp32c3` slugs to `heltec-hru-3601` / `heltec-ht62-esp32c3-sx1262`, and added
  `esp32s3` boards (t-deck-pro, station-g3, tlora-pager). Item B (drag-drop): the profile now declares a
  `chip_uf2_boards` family for the nRF52840 / RP2040 / RP2350 boards (RAK4631, T-Echo, Nano G2 Ultra,
  Pico2, …). A new resolver expander emits their `.uf2` members (never the `.hex`/`-ota.zip` the same zip
  carries), each tagged `flash_method="uf2"`, and the flash engine routes a selected UF2-family chip to the
  (existing) `uf2` backend instead of esptool — so CC never tries to esptool-write a `.uf2`. The legacy
  `MeshtasticProfile` equivalence oracle was kept in lockstep. No new profile file (count unchanged); the
  physical BOOT-drive drag-drop stays HW-gated pending a real RAK4631 / Pico2.
- **nRF Sniffer for 802.15.4 profile — `nrf_dfu` extended to wrap a raw `.hex` (49 profiles).** `nrf802154_sniffer`
  (nordicsemi/nRF-Sniffer-for-802.15.4, NOASSERTION) — passive IEEE 802.15.4 / Zigbee / Thread capture into
  Wireshark (extcap plugin) on the nRF52840 Dongle (PCA10059). Receive-only, `danger=""`. Unlike Chameleon/RNode
  (which ship a ready DFU `.zip`), this ships a **raw `.hex`**, so the `nrf_dfu` backend now runs a pkg-generate
  pre-step first — `nrfutil pkg generate --hw-version 52 --sd-req 0x00 --application <hex>` (classic pc-nrfutil) or
  `adafruit-nrfutil dfu genpkg --dev-type 0x0052 --application <hex>` (the fork) — then flashes the generated
  package. Firmware is in-tree (empty release assets), so the resolver uses `pinned_release` + a pinned-commit
  `raw.githubusercontent.com` URL; the vendor `.hex` is fetched from origin, never re-hosted. Only the dongle is
  CC-flashable (the DK boards need J-Link/SWD). Offsets/SHA/pinned-commit `verify:` until a real dongle flash.
- **PortaPack Mayhem SDR firmware + the new `hackrf_spiflash` flash backend (48 profiles).** New `hackrf_spiflash`
  backend drives HackRF's libusb SPI-flash vendor command (`hackrf_spiflash -R -w <bin>`) — HackRF is
  libusb-addressed, so no serial port; `-R` resets into the freshly written firmware. First (and only) consumer:
  `mayhem` (portapack-mayhem/mayhem-firmware, GPL-3.0) — the flagship HackRF One / Pro / PortaRF PortaPack
  firmware. Whole-flash raw image from `0x0` (no app-offset math); the flashable `.bin` is a **ZIP member** inside
  `FIRMWARE_mayhem_vX.Y.Z.zip`, so the resolver zip-extracts and picks the per-board member (hackrf 1 MB /
  portarf 2 MB / hpro 4 MB). `danger="illegal-tx"` — genuinely dual-use (large legit RX side: ADS-B, POCSAG/ACARS/
  APRS, TPMS, spectrum recon) but bundles TX apps on protected/emergency bands; **label-and-warn, never block —
  CC flashes firmware only and authors no TX payload**. Optional DFU-unbrick sub-flow reuses CC's existing `dfu`
  backend (`1fc9:000c` `hackrf_usb.dfu`). Offsets/SHAs `verify:` until a real HackRF One flash.
- **Wi-Fi Audit tab — the reachable UI for the offline WPA key-recovery pipeline (1.7.0).** The
  `crack_pipeline` + `wordlist_manager` engines were finished + unit-tested but had **no user-facing entry
  point**; a new **Wi-Fi Audit** sub-tab (Operate surface) wires them end-to-end: capture picker →
  live tool-presence check (`detect_tools`) → wordlist picker (installed / BYO / catalog) → **a per-run
  consent affirmation that is never bypassed** (`consent_prompt_text`) → convert (`hcxpcapngtool`) + crack
  (`hashcat` mode 22000 `-a 0` **or** `aircrack-ng`) in a background `QThread` with streamed output + parsed
  result. Dictionary-only (no brute force), and it bundles/installs **no** cracking tools. New
  `tests/test_wifi_audit_tab.py` smoke/integration test drives the surface headless + asserts the consent
  gate blocks a declined run.
- **Round-2 firmware expansion — first two nRF52840 profiles (44 profiles).** `rnode_nrf` (markqvist RNode
  Firmware for RAK4631 / T-Echo / Heltec-T114 — Reticulum LoRa transport, `danger=""`) and `whad_butterfly`
  (whad-team ButteRFly multi-PHY BLE/Zigbee/ESB/Unifying/Mosart/ANT research fw, `danger="lab-only"`). These
  are the **first shipped consumers of the built-but-unused `nrf_dfu` and `uf2` backends** (RNode-nRF flashes
  a whole Nordic legacy-DFU `.zip` via adafruit-nrfutil; ButteRFly offers both a Nordic-dongle `nrf_dfu` path
  and a Makerdiary-MDK `uf2` drag-drop path). Offsets/SHAs stay `verify:` until a real-hardware DFU/UF2 flash
  confirms them (the `nrf_dfu` backend has zero prior HW-validated consumers — that gate is load-bearing).
- **ESP-AT (Espressif) — AT-command Wi-Fi/BT modem firmware profile + parser (1.7.0, bonus).** New `esp_at`
  flash profile + `EspAtProtocol` (`text-cli`, CRLF line-ending) integrate Espressif's official ESP-AT firmware,
  which turns an ESP32 into an AT-controlled Wi-Fi/BT modem. **SAFE** (`danger=""`) — a modem firmware with no
  offensive/RF-attack transmit surface; the parser exposes only read-only AT helpers (`AT` ping, `AT+GMR`
  version, `AT+CWLAP` Wi-Fi scan, `AT+CWMODE?` mode query, `AT+CIFSR` IP/MAC) and parses `OK`/`ERROR`/`busy`
  status, the boot `ready` banner, and structured `+<CMD>:<payload>` responses. The factory image is a merged
  single bin flashed at 0x0 (bench target: ESP32-WROOM-32 on ESP-AT v2.4.0; also S2/S3/C3). **Resolver deviation
  (reality-forced):** Espressif ships the prebuilt bins as per-module ZIPs on `download.espressif.com` (its
  GitHub Releases carry no `.bin` assets), which is outside CC's GitHub-only SSRF fetch allowlist — so the
  profile uses the **`local` resolver** (same model as `custom`): CC flashes the factory bin you extract from the
  official ZIP via the shared local-`.bin` path rather than auto-downloading it. The AT command channel is UART1
  by default; the ROM boot log stays on UART0. Suite green.
- **BlueStress — in-house gated RF-disruption firmware + CC control (1.7.0). STAGED / preview.** New `bluestress`
  flash profile + `BlueStressProtocol` (`text-cli`) integrate LxveLabs' own ESP32+nRF24 firmware (GPL-3.0, derived
  by subtraction from wirebits/nrfBlueNullifier + smoochiee's Noisy-boy), which — unlike the fire-on-boot
  upstream jammers — **boots idle** and exposes a real serial CLI, so CC can honestly present a *gated* operate
  surface instead of a no-op. `get_commands()` returns exactly `Status`/`Bands` (safe) + **`Flood`** (carries
  `danger="illegal-tx"` → consent gate) + **`Off`** (cease is always reachable, ungated). Lab-only / illegal to
  operate on air (FCC 47 U.S.C. 333). **STAGED: the LxveLabs/BlueStress repo + build are not yet published, the
  profile's SHA-256 digests are placeholders, and `verify_sha256` refuses to flash — so it is listed but NOT
  flashable in this build.** When published, CC will flash a pinned SHA-256-verified image and only key/steer/stop
  the upstream engine — it authors no TX and adds no RF power. **Safety hardening:** `"flood"` added to the
  command-string illegal-tx set so a hand-typed `flood <band>` is gated even if it misses a CommandInfo lookup,
  while a *description* containing "flood" (e.g. "probe request flood") deliberately stays lab-only via a new
  `_DESC_ILLEGAL_TX_KEYWORDS` split (no over-flagging of deauth/beacon/probe-flood descriptions). Firmware +
  how-to guide live in `command-center/projects/bluestress` (SAFE-by-default build; the RF primitive is upstream
  wirebits/nRF24, wrapped verbatim behind an owner seam). Suite green (2001 passed). New `nrf-bluenullifier2` profile flashes
  wirebits/nrfBlueNullifier's nRF24L01 variant (a 2.4 GHz jammer — **LAB-ONLY, illegal to operate under FCC
  47 U.S.C. 333**) as a study target, exactly like BlueJammer-V2: CC flashes and exposes **no operate/transmit
  control**. The prebuilt bins are committed in the repo tree (no Release), so they're fetched **pinned to a
  commit SHA** from raw.githubusercontent.com and **SHA-256-verified** before flashing, never vendored. It has
  **no serial interface at all** (fire-on-boot), so the paired `nrf-bluenullifier2` parser is an honest no-op
  (`controlmap` driver, empty command list) rather than a fabricated control surface. Gated `illegal-tx`
  (label-never-block). Build/wiring guide lives in the private ops repo. Real-hardware flash pending the HW gate.
- **WPA/WPA2 + PMKID offline-crack pipeline — core engine (1.7.0).** New `src.core.crack_pipeline`: the host-side
  offline half of the Wi-Fi audit flow that turns a capture you made (PMKID or a full 4-way handshake) into a
  recovered passphrase via your own installed hashcat (mode 22000) or aircrack-ng. **Dictionary-only** by design —
  it never constructs a mask/brute run (`-a 3`), and a test locks that invariant. **Nothing is bundled**: the GPL
  tools (hcxtools/hashcat/aircrack-ng) are detected on PATH and shelled out to; a missing tool gives an honest
  "install it" message, never a fake result. **Consent-gated** with a per-run authorization affirmation on top of
  the first-run legal disclaimer, and **verify-never-fake** — "no handshake in this capture" and "key not in your
  wordlist" are surfaced plainly, and a hashcat hit is read back from the potfile, never asserted. Pure argv/parse
  core is fully unit-tested (17 tests, no hardware, no tools required to run the tests). The Devices-tab UI panel
  and the capture-file retrieval step land next; see `command-center` design doc 08.
- **Wordlist manager for the crack pipeline — install prepackaged or bring your own (1.7.0).** New
  `src.core.wordlist_manager`: because the cracker is dictionary-only, the wordlist *is* the tool. The operator
  can **install a prepackaged list** from a small curated catalog (WPA-specific probable lists, a 10k general
  list, and the classic rockyou) or **use their own** file. Nothing is bundled — prepackaged lists are downloaded
  **on explicit opt-in** from pinned upstream sources (SecLists at a fixed commit SHA; rockyou from the canonical
  naive-hashcat release asset) and **integrity-checked before install**: entries we could pre-hash carry a real
  SHA-256 that must match; the one we could not (140 MB rockyou) is size-verified with a loud "integrity not
  pre-pinned" warning — a made-up hash is never shipped, and a mismatched/rotated download is deleted, never
  installed. Pure catalog/verify/scan core is fully unit-tested (24 tests, no network); the urllib download is the
  thin best-effort layer. Feeds the crack UI's wordlist picker (`scan_installed`) plus a BYO path.
- **Scan-to-export: "Export CSV…" on the Targets tab (1.7.0).** A button in the Targets toolbar writes *every*
  target seen this session — APs, clients and BLE devices from the shared pool — to a CSV file
  (type, SSID, MAC, RSSI, channel, device source, encryption, vendor, first/last seen). It exports the whole
  pool regardless of the search box or the Live-view filter ("scan to export all"). Untrusted SSIDs/vendors are
  run through the same OWASP CSV-injection guard the wardrive logger uses, so a malicious network name can't
  smuggle a spreadsheet formula; numeric RSSI/channel stay raw. Empty pool shows a friendly "run a scan first".
- **"Live view" toggle on the Targets tab (1.7.0).** A checkbox in the Targets toolbar. Off (default) the table shows
  everything discovered this session, exactly as before. On, it filters down to targets seen in the last 45 seconds —
  "what's currently in range" — and rows age out on their own as they go stale (the 3s refresh re-evaluates freshness).
  It combines with the search box and never touches the shared pool: Cross-Comm, macros and the graph still see the
  whole session; this only changes what *this* table displays.
- **Six more firmware profiles — the 07-10 multi-firmware integration pass (36 → 42 profiles).** Each flashes an
  upstream image and adds no capability of its own; all are **SAFE** (`danger=""`, passive/lawful-comms) and were
  added under the owner's "support them all" directive. Flash-only integrations (no fabricated control surface):
  - **`ble_collector` — ESP32 BLE Collector (tobozo).** Passive BLE-advertisement logger to SD (name/MAC/vendor/RSSI)
    with an on-device UI; receive-only, no transmit/pair/attack. App-only upstream image → CC flashes it at **0x10000
    as an app UPDATE** over the device's existing M5Stack/Odroid bootloader (needs a large-app partition scheme; a
    blank board must be seeded with M5Burner first). `pinned_release`.
  - **`drone_mesh_mapper` — Drone Mesh Mapper (colonelpanichacks).** Passive Wi-Fi **Remote-ID (ASTM F3411)** drone
    detector/decoder; node-mode relays detections over Meshtastic. Receive-only (README-confirmed), blue-team
    counter-surveillance. MIT. `pinned_release` (SHA-256 pinned).
  - **`esp32_wardriver` — ESP32 Dual-Band Wardriver (justcallmekoko).** Passive dual-band (2.4 + 5 GHz) Wi-Fi/BLE
    wardriver for the ESP32-C5; logs to SD in **WiGLE CSV**. Passive receive only — no transmit/deauth/injection.
    `pinned_release`, SHA-256-verified.
  - **`meshcore` — MeshCore (mainline).** Hybrid flood+routed **LoRa mesh** (companion role) for ESP32/S3/C3/C6.
    Lawful off-grid comms; no attack/jammer/deauth. `github_release` (merged image).
  - **`nautilus` — Nautilus (n0xa, GPL-3.0).** Lightweight **sub-GHz CC1101 (300–928 MHz) RX/TX** firmware for the
    LilyGo T-Embed CC1101 (ESP32-S3). General-purpose **dual-use RF for authorized use only** (sub-GHz TX can reach
    licensed/region-restricted bands — operate only where lawful); no jammer/deauth feature. `github_release`.
  - **`rnode` — RNode (markqvist, GPL-3.0).** Turns an ESP32/S3 LoRa board (T-Beam, T3S3, Heltec V3, LoRa32, T-Deck,
    XIAO-S3) into a **Reticulum (RNS) radio interface**. LoRa comms transport; no attack/jammer/deauth. User-tunable
    TX radio — operate only on bands you are licensed/authorized for. `github_release`.

## [1.6.9] — 2026-07-08

A bug-fix release that gets core scanning + provisioning working again: discovered APs now actually reach the Targets
list, macros, Cross-Comm and the live network graph, and Dead Man's Switch provisioning works in the shipped app.

### Fixed
- **Scans now populate Targets, Macros, Cross-Comm and the graph.** The Marauder serial parser recognised only the
  legacy `SSID:… BSSID:…` line and the multi-line ESSID/BSSID/RSSI form — not the real v1.12.3 `scanall` single-line
  shape (a bare leading RSSI, a mid-line BSSID and trailing metadata columns, e.g. `-52 Ch: 6 aa:bb:cc:dd:ee:ff ESSID:
  MyNet 11 15`). So every live scan produced only "info" lines, the shared target pool never filled, and the Targets
  tab, macro target-fill, Cross-Comm shared view and the network graph all stayed empty. Because auto-detect routes
  almost every board through the Marauder parser, this looked like "nothing populates, on any firmware." The parser now
  handles that single-line form (including hidden-SSID APs and SSIDs with spaces), sharing the same field extractor the
  wardrive logger already used so the two can't drift apart again.
- **The network graph fills in live as you scan.** It used to read the target pool only when you clicked "Rebuild"; it
  now refreshes automatically (debounced) as APs/clients are discovered, while preserving any layout you dragged.
- **Dead Man's Switch provisioning works in the installed app.** "Provisioning failed: Could not find the NVS partition
  generator (esp-idf-nvs-partition-gen)" — the ESP-IDF tool that bakes the guardcfg NVS image was neither a declared
  dependency nor bundled, so the shipped executable couldn't find it. It's now a dependency and bundled into the build.

### Known / next
- BLE (`sniffbt`) scan lines still need a real-hardware capture to parse correctly — deferred to a bench-validated fix.
- A "live view vs whole-session" toggle, scan-to-export for APs/devices, and deeper multi-firmware feature coverage are
  planned for **1.7.0**.

## [1.6.8] — 2026-07-08

A reliability patch for the one-click in-app updater. "Download & install" verified the new build but often came back on
the old version because the app didn't fully exit for the swap helper; it now installs and relaunches as intended.

### Fixed
- **In-app updater now actually installs and relaunches.** "Download & install" downloaded and verified the new
  build but often came back on the OLD version. On Windows the app quit *gracefully*, which can stall on a live
  background thread (the health monitor, a serial reader, the embedded web server) — so the detached swap helper,
  which waits for the app to exit before it can replace the locked binary, waited forever and never swapped or
  relaunched. The app now exits immediately once the verified update is staged on disk, so the swap + relaunch always
  proceed. The swap also **retries for ~10s** (the just-released exe is frequently locked for a moment by antivirus)
  instead of giving up on the first attempt. And if it genuinely can't replace the binary — an app installed under
  `Program Files` without admin rights — the next launch now surfaces a clear "update didn't finish, move me somewhere
  writable" notice (previously dead code) instead of silently reverting to the old version.

## [1.6.7] — 2026-07-08

A device-focused "rough edges" patch from hands-on v1.6.6 bench testing. The Flock map zooms back out, the
Targets tab fills from any scan (not just a Devices-tab connect), the bundled starter macros use commands the
firmware actually accepts, Device Health stops reading a silent board as a green "connected", "Detect board"
stops presenting a wrong-panel guess as certain, and flashing Marauder no longer silently blanks a
Cheap-Yellow-Display's screen.

### Fixed
- **Flock map: you can now zoom back out.** v1.6.6 fixed zoom-*in* but left the zoom-*out* floor at a fixed value the
  map often sits below (a real camera spread, or the world basemap, frames below it), so the wheel refused to zoom out.
  The zoom-out floor is now "fit the whole scene," so you can always pull back to see everything.
- **Macros: starter macros now use real firmware commands.** Several bundled macros sent commands the firmware no
  longer accepts, so they silently did nothing (and downstream steps then acted on an empty list): Marauder's
  `scanap`/`scansta` (removed in firmware v1.12.3 — now `scanall`), a `select -a -f <ssid>` form that never existed
  (now `select -a <index>`), and `sniffpmkid -c <ch>` (now set the channel first, then `sniffpmkid`). The Flipper
  device-info macro's `device_info` is now recognized in the command catalog, and the mislabeled GhostESP "BLE spam"
  template was removed (GhostESP has no BLE-spam command; that capability lives on Marauder/ESP32-DIV). A new test
  validates every bundled macro against its firmware's real command set so this can't drift again.
- **Device Health panel now tells the truth.** Firmware showed a permanent "unknown", and a board whose USB port
  stayed open but whose firmware had gone silent (hung or mid-flash / mis-flashed) still read as a green "connected"
  with a ticking Last Seen. Health now reads the real firmware from the connect-time handshake, and a silent board
  reads "no-reply" with its Last Seen frozen instead of a false green.
- **"Detect board" no longer presents a wrong-panel guess as certain.** The CYD display probe stamps high confidence
  from a liveness check even when it couldn't read the panel's controller ID, so a 2.8" ILI9341 whose read-ID fails to
  latch was mis-identified as a 2-USB ST7789 (wrong build → blank screen) and silently pre-selected. Detect now spots
  that unsupported fallback guess, reports it as low-confidence, and warns you to verify the variant (naming the ILI9341
  and Guition alternatives) instead of presenting a guess as a sure thing.
- **Targets tab now populates from any scan, not just a Devices-tab Connect.** Discovered APs/clients only reached
  the shared Targets pool when you connected a board on the Devices tab — wardriving or broadcasting scanned fine but
  left Targets empty. The ingestor now attaches automatically to every connection the app opens, so any device's scan
  feeds Targets (and the cross-device AutoRouter) no matter which tab opened the link.
- **Flashing Marauder no longer silently blanks a display board's screen.** Marauder's "Auto" default is
  per-chip, and on a classic ESP32 — which is every Cheap-Yellow-Display, and which a USB-serial bridge can't
  tell apart from a plain ESP32 — that default is the generic ILI9341 build, the wrong display driver for most
  CYD panels, so the screen stays dark after a flash that otherwise reports success. Now, when a Marauder "Auto"
  flash lands on a board we can't positively identify, Cyber Controller asks you to confirm the generic build or
  pick your exact panel (or run "Detect board") first, and reminds you to re-pick and re-flash if the screen
  comes up blank. A board we *can* identify as an ESP32-S3/S2/C3 flashes its correct build with no prompt. The
  Batch Queue now carries the board/variant you picked with each job (it used to drop it and fall back to the
  generic default) and runs the same one-time check before a queued Marauder "Auto" flash.

## [1.6.6] — 2026-07-08

A performance + quality patch driven by hands-on use of the v1.6.5 Flock map. With a large camera set the map now
stays smooth, frees its memory when you leave the tab, and scroll-to-zoom works again — plus a command-line check for
firmware updates. (The one-click in-app updater — download & install a new release in place, keeping your data — is
already built; this is the first release you can update *to* with it.)

### Added
- **Check your cached firmware for updates from the command line.** `cyber-controller --check-firmware-updates` looks up
  the latest GitHub release for each firmware you've already downloaded into the vault and prints any that have a newer
  version than what you have cached (name, cached version, latest version). Read-only — it never downloads or flashes
  anything — so you can see what's stale before deciding to refresh a profile.
- **Flock map: free its memory while you're on another tab.** A new **Unload when off-tab** toggle (on by default) drops
  the map's cameras and basemap when you leave the Flock tab and rebuilds them from the retained scan when you come back —
  so a big camera set stops eating CPU/RAM in the background. A live scan keeps recording either way; turn the toggle off
  to keep the map warm across tab switches.

### Fixed
- **Flock map: big scans stay responsive.** The map now draws every camera through a single layer that renders only the
  dots currently in view, instead of creating one scene item per camera. A large scan — a wide DeFlock export can be tens
  of thousands of points — no longer bogs the map down: off-screen cameras cost nothing to pan or zoom past, and the map
  holds a lightweight point list in memory rather than thousands of graphics items.
- **Flock map: scroll-to-zoom works again.** On a map with a lot of cameras — or with the world basemap on —
  "Reset view" frames the whole set at a zoom level well below the map's minimum, and the old zoom limiter then
  refused *every* wheel notch in both directions, so the wheel did nothing ("I can't scroll to zoom"). The limiter
  now only blocks a notch that would push *further* past a limit, so you can always scroll back toward a normal zoom.
  The wheel notch is also consumed so it can't leak through to the scrollbars as a stray pan.
- **"Check for firmware updates" now only checks firmware you actually have.** The update check walked the *entire* 30-plus
  profile catalog and hit GitHub once per profile — so it made ~30 network calls even when the vault was empty, and it
  reported every never-downloaded firmware as an "available update." It now checks only the profiles you've cached (one
  call each, none if nothing is cached), which is both correct and far faster. (This was dead code with no caller before;
  it's now wired to the new `--check-firmware-updates` command.)

## [1.6.5] — 2026-07-08

The 1.6.4 hands-on + real-hardware testing batch: Flock map/GeoJSON work, a wardrive/backup CLI toolkit, and fixes for
issues found by connecting real devices — including a Marauder AP scan that was silently logging nothing.

### Added
- **Summarize a wardrive capture from the command line.** `cyber-controller --wardrive-summary <wigle.csv>` reads a WiGLE
  CSV and prints the headline stats — how many networks, the open/WPA/WEP split, the 2.4 vs 5 GHz mix, how many were
  logged with a GPS fix, the strongest/weakest signal, and the busiest channels — so you can see what a drive collected
  without loading it into another tool. Tolerant of a partial or hand-edited CSV.
- **List your firmware backups from the command line.** `cyber-controller --list-backups [DIR]` prints the backups in a
  folder (the default backups folder, or any `DIR` you point it at) with the chip, size, date, and recorded SHA-256 read
  from each `.meta` sidecar — flagging any whose flash size was only assumed. Pairs with `--verify-backup` so you can
  inventory, then integrity-check, a backup you're about to restore from.
- **Check a backup's integrity from the command line.** `cyber-controller --verify-backup <backup.bin>` re-hashes a
  firmware backup and compares it to the SHA-256 recorded in its `.meta` sidecar, so on-disk corruption or truncation is
  caught *before* you rely on that backup to restore a board. It prints the verdict (intact / corrupt / uncheckable),
  carries through the "flash size was assumed" caveat, and exits non-zero unless the file is intact — so a restore
  script can gate on it.
- **Export the Flock map to CSV.** An **Export CSV…** button on the Flock map saves the cameras currently shown — whether
  from a live scan or a loaded `cameras.geojson` — as a spreadsheet-friendly CSV (MAC, lat, lon, SSID, RSSI, channel,
  frequency, first/last seen, count). GeoJSON is great for mapping but awkward in a spreadsheet; this lets you sort,
  filter, and cross-reference located cameras in Excel/Calc or feed them to another tool. SSIDs are untrusted broadcast
  text, so the export neutralizes spreadsheet formula injection (a name like `=…` can't run on open).
- **Backups now save a self-documenting sidecar.** Each flash backup writes a small `<backup>.meta` next to the `.bin`
  recording the chip, port, flash size (and whether that size was actually *detected* or a 4 MB guess), a SHA-256, and a
  timestamp. So a backup is no longer an opaque blob you can't identify weeks later, and a restore/listing flow can read
  the chip and warn you when an image may be truncated — instead of re-detecting or restoring blind. The sidecar is
  best-effort: if it can't be written, the backup itself still succeeds.
- **Tab icons across the whole app.** Every tab now carries its LxveLabs monochrome icon (Connect, Operate, Network,
  Flash, Firmware, Devices, Flock Map, Wardrive, and the rest), tinted to the violet accent. The icons are the SVG set
  already in the repo, loaded through a small `currentColor`-aware helper so a single set themes correctly and a popped-
  out-then-restored tab keeps its icon. A missing icon file degrades to a text-only tab rather than crashing.
- **The Flock camera heatmap is now a traversable map with a world basemap.** The map used to fit every camera into a
  fixed frame with no way to move around it. You can now **drag to pan and scroll to zoom** (zoom anchors under the
  cursor, like a slippy map), and a **Reset view** button re-frames all cameras after you've explored. A new **World
  basemap** toggle draws a muted world-countries outline (Natural Earth 1:110m, public domain — bundled with the app, no
  download) beneath the detections, so a scan sits in real geographic context; zoom and pan out to see the whole globe,
  or switch it off for a plain map. Loading a scan frames the fresh set; a live scan keeps your current pan instead of
  yanking the view on every new detection. (A bundled DeFlock dataset and an optional "update" button are coming next.)
- **"My location (GPS)" on the Flock map.** A new toggle (off by default) drops a "you are here" pin at your real-world
  position while a GPS is streaming during a live scan. It reuses the scan's existing GPS feed — no second reader — and
  projects the live fix through the same map projection as the cameras, so your pin sits correctly among the detections
  and the world basemap. The pin stays a fixed on-screen size as you zoom, like a real map marker. A **Follow** toggle
  keeps the map centred on you as the fix updates (like a car sat-nav), a **Center on me** button recentres on demand,
  and the pin greys out once the scan stops so a stale position doesn't read as your live one. The toggle now also
  works **without a scan running**: turn it on with a GPS port selected and the map opens a light standalone GPS reader
  just to track your position — so you can use the Flock map as a plain live GPS map, then start a scan any time (the
  scan takes over the GPS port cleanly, and tracking resumes on its own when the scan ends).
- **The Flock scan now shows GPS quality, not just position.** While a scan is running, the fix readout reports the
  satellite count and HDOP (horizontal dilution of precision) next to your coordinates — so a weak or degraded fix is
  obvious at a glance instead of looking identical to a strong one. The figures are parsed from the receiver's GGA
  sentences (and read as unknown on an RMC-only or older receiver that doesn't send them).
- **New firmware profile: WiFiDuck (`SpacehuhnTech/WiFiDuck`).** Fills a category the profile set didn't cover — a
  Wi-Fi-controlled BadUSB / keystroke-injection tool (from the same author as the ESP8266 Deauther we already ship).
  Cyber Controller flashes the **ESP8266 "Wi-Fi backpack" half** (the web UI + serial CLI that stores and runs Ducky
  Script) as a single merged image at `0x0` via `esptool`, the same path as the ESP8266 Deauther. It offers the two
  ESP8266 board images from the upstream release (DSTIKE WiFi Duck, Malduino W) and skips the ATmega/SAMD21 HID-companion
  assets, which are flashed separately with their own `.hex`/`.uf2`. Keystroke injection is labelled/gated by the safety
  layer and never performed by the app; authorized testing only. **33 firmware profiles** now ship.
- **New firmware profile: ESP32 WiFi Penetration Tool (`risinek/esp32-wifi-penetration-tool`).** A classic-ESP32 WiFi
  attack/recon toolkit (deauth, PMKID + WPA handshake capture, PCAP, all over a self-hosted web UI). Multi-file
  ESP-IDF release flashed at the offsets the upstream README documents — bootloader @0x1000, partition-table @0x8000,
  app @0x10000 — with all three assets SHA-256-pinned from the v1.0 release. Deauth capability is labelled/gated by the
  safety layer and never operated by the app; authorized testing only. **32 firmware profiles** now ship.
  **Hardware-validated** on a real 4 MB ESP32 (COM19): backed up → flashed the 3 files → booted cleanly (the firmware's
  `ManagementAP` + web server came up, no bootloop) → restored the original image (esptool write-verified; a byte diff of
  the read-back showed only the single NVS/RF-calibration sector the restored firmware rewrites on boot, app untouched).

### Fixed
- **A live Marauder AP scan now actually logs its access points (was silently dropping every one).** Found during a
  real-hardware SniffAP test: modern Marauder (v1.12.3+) `scanall` prints each AP as `-71 Ch: 2 <bssid> ESSID: <name>`,
  with the signal strength as a bare leading number and no `RSSI:` label. The scan-line reader only recognized a labelled
  `RSSI:` field, so it never saw a signal value and — because an AP is only recorded once both a BSSID and an RSSI are
  known — discarded every AP a real scan reported, leaving an empty wardrive/WiGLE capture. The reader now also reads that
  leading `-NN Ch:` form, and strips the two trailing metadata columns `scanall` appends so the network name is clean
  (`SpectrumSetup-7272`, not `SpectrumSetup-7272 11 15`) while names containing spaces or digits are preserved. Verified
  end-to-end against a physical Marauder (25-30 real APs parsed per scan). Labelled and pipe-delimited formats are unchanged.
- **A GPS position is no longer thrown away when the receiver reports a garbled altitude.** The GGA parser read the
  altitude field without its own guard, so a non-numeric altitude raised an error that discarded the entire fix — even
  though latitude, longitude and fix-quality were perfectly good. Altitude (like the satellite count and HDOP) is now
  parsed defensively on its own: a bad value just leaves that one figure unknown instead of dropping your position.
- **Starting a Flock scan while "My location (GPS)" was tracking no longer briefly greys your position pin.** The
  standalone GPS reader's "stopped" notification is delivered across threads, so it could land just after a scan had
  taken over the GPS feed and grey the pin the scan was actively updating (and, in a rarer race, drop the handle to a
  freshly-started reader). The handler now only greys the pin when nothing else is feeding it and only clears the reader
  it actually owns.
- **HaleHound's OTA update image is no longer offered as a full flash.** The profile matched every `.bin` release
  asset and treated it as a merged image written at 0x0 — including the app-only OTA update image, which belongs to the
  running firmware's over-the-air update path, not a cold esptool flash at the bootloader offset. The OTA image is now
  excluded from the asset list, so only the full merged image is offered (mirroring the M5PORKCHOP profile's narrower
  match). No current HaleHound release ships firmware `.bin` assets, so this is a latent fix.
- **"Detect board (CYD)" can no longer collide with a flash/backup/erase on the same port.** The detection probe-flash
  ran esptool directly without reserving the port, so starting a Backup, Erase, or Flash while detection was running
  could put two esptool processes on one UART — a brick path. Detection now goes through the same per-port busy-guard as
  flashing: it's refused if the port is already in use, it blocks the other operations while it runs, and the
  Backup/Erase buttons are disabled for the duration.
- **SD-card write verification now reflects the physical card and works from a non-root shell (Linux).** The read-back
  opened the block device buffered, so a verify could pass by reading the just-written data straight from the Linux
  page cache (RAM) rather than the card — a corrupted write could be reported as verified. It also read with an
  unprivileged `open()` even though the write ran via `sudo dd`, so a good write from a non-root shell was reported as a
  permission failure. The read-back now drops the device's cached pages first (`posix_fadvise DONTNEED`) and, when the
  write needed `sudo`, reads back with `sudo dd iflag=direct` at the same privilege. macOS already read the unbuffered
  `/dev/rdisk`; Windows is unaffected. (Helper logic is unit-tested; on-card behaviour needs a Linux bench to validate.)
- **A firmware profile can no longer accidentally disable the flash-size safety check.** The engine forces
  `--flash_size detect` (which patches a merged image's header to the board's real size). If a profile had put its own
  `--flash_size` in `extra_args`, esptool's last-flag-wins would have silently overridden that safeguard and re-opened
  the wrong-size bootloop. A profile `--flash_size` is now stripped from extra_args with a warning, so the engine's
  `detect` always wins. No shipped profile did this — it's a latent-footgun fix.
- **RayHunter now downloads on Intel Macs.** The ADB firmware picker mapped an Intel Mac's `x86_64` architecture to
  asset names containing `x86_64`/`x64`, but RayHunter's macOS builds are named `macos-intel` — so Intel-Mac users
  matched no asset and the install silently found nothing. `intel` is now a recognized token for x86_64/amd64 (Apple
  Silicon already worked via `arm`).
- **A merged firmware built for a bigger flash chip now warns instead of silently leaving a dead board.** Flashing a
  merged-single-bin build (say a 16 MB Bruce build) onto a smaller board (a 4 MB ESP32) wrote and verified fine, then
  reported "Flash complete" — but the board just bootlooped, because the image's own bootloader still claimed 16 MB of
  flash (`--flash_size detect` only patches the header at the write offset, not one buried inside a merged image). The
  flasher now reads the size the image demands, compares it to the size esptool detects on your board, and if the image
  is too big it says so plainly ("built for 16 MB, your board has 4 MB — it likely won't boot") instead of claiming a
  clean success. Multi-file firmwares (Marauder, most others) were never affected. The same check now covers all three
  ways a merged image reaches the chip — downloaded firmware, a local `.bin` you point the app at, and firmware flashed
  from the offline vault (the offline path exclusively handles merged blobs, so it was the most exposed).
- **You're no longer trapped on the Connect ▸ Devices tab.** With a device selected, the 3-second sidebar
  refresh re-selected it in the device list, and that programmatic re-selection fired the same signal a real
  click does — so the main view snapped back to Connect ▸ Devices a couple seconds after you switched to any
  other tab. The refresh now re-selects silently; navigating away sticks.
- **Flashing no longer crashes with `OSError: [Errno 22]` in the installed (windowed) build.** The packaged app
  runs esptool as a child of itself, and a windowed build has no console — so esptool's progress `print()` wrote
  to an invalid stdout and crashed, masking the real connect result (e.g. a board that needs BOOT held). The
  esptool dispatcher now binds valid output streams before running esptool, so its progress and errors reach the
  flash log instead of taking the whole flash down.
- **The flash progress bar now climbs 0→100% instead of jittering 0–9.** esptool reports progress as `X.Y%`
  (e.g. `27.6%`) and the parser was grabbing the digit right before the `%` — the tenths (`.6` → 6) — because the
  decimal point broke the digit run. It now reads the whole-percent integer, so a flash shows real progress.
- **"Clear Terminal" now clears the terminal you're actually looking at.** The command-palette "Clear Terminal"
  only cleared the Devices tab's terminal, leaving the always-visible bottom terminal panel — the one on screen
  most of the time — untouched, so it looked like it did nothing. It now clears both.
- **The Flash tab can no longer start two flashes at once.** During a single flash, the "Flash Queue" button
  stayed clickable and the batch guard never checked the single-flash worker — so starting a batch mid-flash
  launched a second, concurrent esptool run. Both flash buttons are now disabled for the duration of any flash,
  and each flash path refuses to start while the other is running.
- **Quitting mid-wardrive now stops the scan instead of leaving the board scanning.** On exit, the Wardrive and
  Multi-Board Wardrive tabs weren't shut down, so the ESP32 kept scanning, its serial ports weren't released
  cleanly, and the WiGLE CSV wasn't closed. They now stop the capture — sending the firmware's stop command —
  when the app closes.
- **The Secure Container no longer claims to encrypt data it doesn't.** Its Settings copy advertised at-rest
  encryption for "logs, sessions, captures", but only recorded macros are actually written through the container
  (logs stay in memory for the session; wardrive CSVs are plaintext by design). The description and checkbox now
  state what's genuinely protected — your saved macros — so you aren't misled into thinking logs or captures are
  encrypted. The encryption itself (AES-256-GCM, gate-keyed) is unchanged.
- **GhostESP's GPS command now works.** The command palette and the Device menu sent "gps info" (two tokens), but
  GhostESP's command is the single token "gpsinfo" — the board treated "gps" as an unknown command and never
  returned GPS status. Both now send "gpsinfo", matching the token the app's own wardrive macro already uses.
- **The in-app User Guide now matches the app.** Its "Available Settings" list advertised five controls that don't
  exist (Auto-reconnect, Theme, Macro directory, Health polling interval, Cross-comm auto-routing) and left out the
  real ones; the Performance section named a per-device "temperature" readout the Health table has no column for.
  The guide now lists what Settings actually offers (serial/flash baud, updates, safety, Access Gate, Secure
  Container, firmware vault) and describes the real Health columns.

## [1.6.4] — 2026-07-07

A ten-round exhaustive adversarial debug and hardening pass on top of the 1.6.1–1.6.3 security reviews. Where
those reviews swept the security surface, these rounds went over the whole tool one failure class at a time —
round 1 correctness, round 2 silent failures, round 3 concurrency and resource handling, round 4 security,
round 5 integration wiring, round 6 the flash/serial path, round 7 edge cases and junk input, round 8
state-persistence durability, round 9 honest functionality (every advertised control actually does something),
and round 10 test quality — and folded in a hardware-validated CYD connect fix and a single canonical Flock data
folder. Every fix is guarded by a test. No firmware changes; no breaking changes to profiles or the CLI.

### Security
- **Changing the admin password no longer locks you out of the vault.** A password change committed the new
  gate verifier without re-keying the encrypted vault's `password` keyslot, so the new password passed the gate
  but could no longer unwrap the vault — a permanent, silent lockout. The vault is now re-keyed first (with the
  physical key or the current password) and the gate password is committed only if that succeeds; if the vault
  can't be unlocked, nothing changes. Fixed in both the CLI (`--set-admin-password`) and the Qt gate dialog.
- **No AES-256-GCM nonce reuse on a shared node link.** `NodeLink` sealed and wrote outbound frames without a
  lock, so two threads writing the same link could seal under the identical (epoch, counter) and reuse a GCM
  nonce. Seal-and-write is now serialized per link.
- **A duress wipe only reports success when the secrets are actually gone.** `trigger_duress_wipe()` returned
  True if it merely *attempted* a wipe; a file held open or read-only was silently skipped while the owner was
  told their secrets were destroyed. It now verifies each target is gone and reports failure otherwise.
- **Serial writes no longer echo secrets to the log.** `SerialConnection.write` — the single funnel for every
  outbound command, including the Dead Man's Switch unlock password — logged the raw text at DEBUG and could
  persist that password to any `--log-file`. It now logs only a byte count.
- **Spreadsheet formula injection blocked in the wardrive export.** A Wi-Fi SSID beginning with `=` / `+` /
  `-` / `@` (attacker-chosen) was written verbatim to the WiGLE CSV and would execute on open in Excel or Calc.
  Leading formula triggers are now neutralized.
- **The admin password is redacted from the adb command echo.** `--admin-password <value>` was printed to the
  on-screen log the UI can export. The value is now masked.
- **The audit trail is owner-only on POSIX too.** The persisted trail (web auth usernames, flash/connect/serial
  records) was created world-readable off Windows, where the NTFS ACL is a no-op. It is now `0600` from the
  first byte.
- **A rogue board can't forge terminal markup.** Device serial output was appended to the Qt terminals as rich
  text, so a board emitting `<span>` or `<b>` could spoof output — including a fake green `[DMS] Authenticated`
  banner. Device lines are now escaped everywhere they're shown.
- **The experimental Network tab's attack actions now clear the safety gate.** Deauth / Beacon Clone / Karma
  actions fired with no confirmation, bypassing the arm gate the rest of the app enforces. They now go through
  the same danger classifier and confirmation.
- **OS and Tails metadata fetches re-validate every redirect hop.** A 302 could bounce a metadata fetch off the
  host allowlist and let an attacker-chosen endpoint serve the SHA-256 the flow trusts. Redirects are now
  followed manually and re-checked against the allowlist (SSRF), matching the download paths.
- **The offensive-macro arm gate catches HaleHound's attack verbs.** Underscore-joined commands (`wifi_deauth`,
  `ble_cinder`, `subghz_replay`, `mousejack`, `protokill`, `tag_disrupt`) slipped past the prefix list and
  played without the arm prompt. They're now covered.
- **The offline vault refuses to cache firmware it would flash wrong.** App-only, multi-file-offset firmware
  (Marauder, ESP32-DIV) needs a boot chain at per-file offsets the vault can't store; caching it would let an
  offline flash write an app image at 0x0 with no boot chain and brick the board. That firmware is now refused
  for caching (fail closed).
- **The vault survives a power loss mid-write.** The vault header (`vault.hdr.json`, the only wrapped copy of the
  data-encryption key and salt) and the vault blob (`vault.enc`, every node key and the secure-container key) were
  rewritten in place with a truncate-then-write; a crash between the truncate and the finished write left a 0-byte
  or partial file, and with no second copy that permanently destroyed every key. Both now write atomically (temp
  file + fsync + `os.replace`), so a crash leaves either the old complete file or the new one — never a torn hybrid.
- **A stale-stolen node-provision lock can't delete its successor.** The provisioning lock released by filename, so
  a lock stolen as stale and recreated by another process was then unlinked by the original holder — collapsing
  mutual exclusion and opening an AES-GCM nonce/epoch reuse window on a concurrent reservation. The lock now removes
  only the exact file it created, matched by device and inode.
- **A corrupt vault header fails closed instead of crashing management commands.** A truncated, empty, or non-object
  `vault.hdr.json` raised a raw `JSONDecodeError`/`KeyError` out of no-auth commands like `--gate-status`. An
  unreadable or incomplete header (including one missing its `salt`) is now treated as empty — the encrypted data
  stays sealed and the caller sees a clean state rather than a stack trace.
- **The physical-key password change can't wedge.** Changing the admin password tried to unwrap with the USB key
  even when the vault had no key slot (a gate/vault drift), which skipped the current-password fallback and
  permanently blocked the change while the key was inserted. It now uses the key only when it is genuinely a vault
  factor.

### Fixed
- **Connecting to a CYD no longer blanks its screen.** On CYD panels without the auto-reset transistor pair
  (2.8" 2-USB, Guition), opening the port with pyserial's default asserted DTR/RTS dropped the ESP32 into ROM
  download mode the instant you connected — the firmware stopped and the display went dark until a power-cycle.
  The port is now opened with DTR/RTS deasserted, on both the connect path and the enumeration probe, and the
  **CH340K** (`1A86:7522`) on those newer boards is recognized as an ESP32/CYD instead of a generic USB device.
  Hardware-validated.
- **Flock scans now live in one place you can find.** The live-drive checkpoint, the Load dialog's start folder,
  and a new **Open data folder** button all point at a single `~/.cyber-controller/flock` directory, so captures
  stop scattering to wherever the OS last browsed.
- **Firmware update checks stop crying "update available" forever.** A tag containing a character like `+` was
  sanitized before caching but not before the "is it cached?" test, so a cached firmware was perpetually reported
  as an available update. The comparison now sanitizes both sides.
- **A successful RTL8720 write is no longer mislabeled a skipped no-op.** The SPI "unprotect" failure markers
  were broad substrings that also matched the normal step name and the success word, so a good write reported as
  skipped. The markers are now failure-specific.
- **A missing-RSSI sighting can't hijack a mapped location.** Wardrive treated RSSI 0 (the parser's "no reading"
  sentinel) as the strongest signal, letting a reading-less sighting overwrite a genuine strong one. Sentinel 0
  now ranks below any real reading, in both the single- and multi-board sessions.
- **ESP32-DIV deauth events capture the target MAC.** An optional trailing capture group let the lazy match
  settle on empty, so the MAC was always `None`. A target-less deauth still registers; a targeted one now carries
  the MAC.
- **Auto-detect stops confusing Marauder with its siblings.** Marauder's fingerprint leaned on tokens GhostESP
  and ESP32-DIV also print (`scanap`, `BSSID:`, `Deauth sent`), misidentifying them. It now keys only on
  Marauder-specific tokens.
- **Device View writes to GhostESP / ESP32-DIV again.** The skin-to-protocol map used the wrong names, so the
  send silently refused. Corrected to the real protocol names.
- **The Targets table sorts RSSI and Channel numerically** (1, 2, 10) instead of lexicographically ("1", "10",
  "2").
- **A requested erase that fails now fails the flash.** Both the batch flasher and the flash engine continued
  after a skipped or failed `erase_first`, leaving stale NVS/SPIFFS behind under a "Flash complete". A failed
  wipe now aborts the write.
- **A stalled flasher or adb child can't hang the app.** `adb` and `rtltool` output is now drained on a side
  thread so the wall-clock timeout fires even when a wedged child emits nothing and never exits — previously it
  held the serial port until the next operation failed "port busy".
- **Concurrent flashes of the same firmware can't corrupt the shared cache.** A second flash could truncate the
  cache file mid-read of the first, flashing a corrupt/empty image while esptool still reported success.
  Downloads are now serialized per path, written atomically, and reused.
- **The audit chain survives concurrent writes.** Two threads appending at once could read the same predecessor
  and mint two entries with an identical `prev_hash`, breaking the tamper-evident chain. `record()` is now
  serialized.
- **Node teardown persists a consistent replay cursor.** The link is now detached before its replay head is
  read, so an epoch rotation mid-read can't persist a torn (epoch, highest) pair that spuriously rejects frames
  after a restart.
- **CYD detection reports "no response" instead of a false "bare ESP32".** A probe that produced no report block
  was read as a confident bare-ESP32 result; it now surfaces as a distinct no-response outcome so the safeguard
  isn't defeated.
- **A failed Windows self-update surfaces instead of pretending it applied.** If the swap couldn't replace the
  running binary, the helper now leaves the verified staged build in place, drops a breadcrumb the next launch
  reports, and relaunches the old build. The swap script is also written in the console OEM code page, so an
  accented Windows username no longer aborts the update after staging.
- **The Health tab's Device Health table populates.** The monitor wasn't wired to the device lifecycle, so the
  per-device table stayed permanently empty. It now registers and unregisters devices on connect/disconnect and
  back-fills what's already attached.
- **Macros recorded from the Devices tab and the Tk terminal capture their steps.** Neither send path notified
  the recorder, so a recording made there captured nothing. Both now feed it. Tk macro playback also reports
  completion and errors again (async playback had left the status reset and error dialog dead).
- **Serial and flash baud settings are honored.** The Default Baud Rate (Settings ▸ Serial) and Flash Baud Rate
  (Settings ▸ Flash) reached `settings.json` but no code read them, so lowering the baud for a marginal CH340K or
  long-cable board did nothing. The Qt, Tk, and web connect/flash paths now use them, and inert Settings controls
  with no consumer (connection timeout, flash mode, verify, auto-backup) were removed.
- **Flash restores are byte-exact.** `restore_flash` wrote with `--flash_size detect`, which re-patched the
  header byte on chips whose bootloader lives at 0x0 (S3 / C-series / H2) and tripped a spurious verify mismatch.
  It now writes with `--flash_size keep`.
- **A bad chip name is rejected loudly.** The Dead Man's Switch builder canonicalizes and validates the chip; an
  unrecognized spelling of an S3/C3/C6/H2 part used to default silently to the classic 0x1000 bootloader offset
  and soft-brick the board.
- **The web Flash page accepts a plugged-in port.** A device present at server start was never hot-plug-
  registered, so every port the page offered was rejected. The check now also accepts a live-scanned port.
- **More wiring fixes:** a sidebar device pick now drives the Devices tab; Flock live-scan diagnostics (including
  the busy/denied-port failures) show in a log pane instead of vanishing; the mode badge paints at launch; a
  broadcast to mixed-firmware ports re-stamps each port's line terminator so a CR-only Flipper isn't sent an
  ignored LF; the Tk auto-connect-on-detection toggle is consumed; F5 refresh and Ctrl+Tab tab traversal are
  wired in the Tk UI; and RayHunter install fails instead of reporting success when a config/init push fails.
- **Corrupt or hand-edited config no longer crashes the app.** A `null`/non-list `firmwares` or `hardware` in a
  saved loadout, a `null` settings section (`serial`/`ui`), a vault index that is valid JSON of the wrong type, and
  a non-object web-socket payload are now coerced or failed-open on the way in instead of raising `TypeError` /
  `AttributeError`.
- **Firmware images with an uppercase extension flash again.** Image discovery matched extensions
  case-insensitively but decompression didn't, so an asset like `Foo.IMG.XZ` was discovered, offered, and fully
  downloaded, then failed only at the decompress step — advertised as flashable but never flashable. Both sides now
  match case-insensitively.
- **The cache index, settings, and in-flight downloads survive an interrupted write.** The firmware cache index
  (`vault_index.json`), `settings.json`, and streamed firmware downloads now write atomically (temp + fsync +
  `os.replace`); previously a crash mid-write could discard the whole cache catalog, corrupt settings, or leave a
  torn image in the shared cache path.
- **Saving Settings no longer reverts a change made elsewhere.** The Settings save re-reads the file on disk and
  overlays only its own widget-backed keys, so a concurrently-persisted choice (update-suppression, interface mode,
  loadout) isn't rolled back; the access-gate config and factor writes are likewise hardened against clobbering a
  concurrent failed-attempt counter.
- **The Batch Queue's "Flash Queue" now flashes the queue.** The Batch Queue card and the in-app How-To advertised
  sequential multi-device flashing, but there was no control or logic to run it — you could add jobs and never flash
  them. A **Flash Queue** button now flashes each queued (port, profile) one at a time down the exact single-flash
  path.
- **The Firmware Vault directory and Default Port settings are honored.** The Settings ▸ Vault directory reached
  `settings.json` but every vault still used the hardcoded default (which itself pointed at a folder the code never
  used), and the Tk Default Port was saved but the Flash tab always preselected the first enumerated port. Both are
  now read and applied.
- **Inert Cross-Comm toggles replaced with the truth.** The "Auto-share discoveries" and "De-duplicate by MAC"
  checkboxes had no consumer — unchecking either did nothing. That behavior is intrinsic and always on (targets are
  keyed by `type:mac`), so the toggles are gone and the card now says so plainly.
- **"Deauth this client / AP" target actions resolve.** The Marauder, ESP32-DIV, and GhostESP scan parsers didn't
  tag discovered stations/APs with the discovery-order index those actions select on, so the actions were silently
  dropped. The parsers now assign the index — and where a firmware genuinely emits no client event (GhostESP), that
  limit is documented rather than faked.
- **OS-image signature checks defer to the hash when the key isn't imported.** A detached/clear-signed GPG check
  hard-refused a genuine image when the pinned key simply wasn't in the keyring (the normal first-run state); it now
  returns "undetermined" and falls through to the SHA-256 check, while a real bad signature still hard-refuses.
- **The Targets tab's actions clear the safety gate,** matching the Devices and Network tabs — an attack action
  floors to a lab-only confirmation before it runs.
- **Macros capture commands driven from the Device View and Remote tabs,** which bypassed the recorder before and
  were silently lost on replay.

### Added
- **SD-card imaging for the Raspberry-Pi profiles** (Pwnagotchi / RaspyJack / Kali ARM) via `discover_sd_images`
  / `flash_sd_image` — removable-target-only, read-back verified, and confirmation-gated (the whole drive is
  erased). The serial Flash path only carries a port and can't drive this; these methods do.
- **A 28-icon monochrome UI set** (`currentColor` line-art SVGs, generated) plus the LxveLabs contact rebrand
  (Discord + Proton). Internal/branding only.

## [1.6.3] — 2026-07-06

Four more adversarial review passes after 1.6.2 (six in total), each verifying the last. They closed a few
more security/safety gaps — including two that *completed* fixes shipped in 1.6.2 and one regression the
convergence checks caught before it could ship. No firmware changes; no breaking changes.

### Security
- **A gate clear now disarms the opt-in duress wipe.** `--clear-gate` removed the password/key but left the
  destructive `wipe_on_failures` threshold in the config; since a cleared gate reprovisions as
  unauthenticated first-time setup, the new gate silently inherited it — a few failed unlocks could
  irreversibly wipe secrets the owner never re-opted into. Clearing the gate now fully disarms the wipe.
- **The web remote can't pre-load the local duress wipe.** `allow_wipe=False` stopped the network path from
  *firing* the wipe, but it still advanced the shared counter that *arms* it. The wipe is now armed by a
  separate counter only local failures advance.
- **Offensive-macro arm gate catches real attack commands.** The play-time arm confirmation was matched by
  exact first-token, so `beaconspam`, `karma`, `AT+DEAUTHIDX`, `probe` played *without* the prompt and began
  transmitting. It now prefix-matches the firmware protocols' actual attack commands.
- **AutoRouter proximity floor** — an unknown RSSI (sentinel 0) no longer slips past an explicit `min_rssi`,
  so a "nearby APs only" rule can't fire an attack on out-of-range targets.
- **Web terminal** no longer leaks raw serial/OS exception text to the client (parity with the HTTP path).

### Fixed
- **A shared gateway dongle stays alive while any node rides it.** The refcount now counts NodeLink borrows,
  so disconnecting the Devices tab can't close the dongle out from under an attached node — and (from a
  follow-up pass) borrowing an *untagged* gateway no longer closes it on detach either.
- **`--deadman-setup` (and other CLI subcommands) run while the GUI is open.** The single-instance lock
  guarded every subcommand and returned success when blocked, so the DMS provisioning the GUI directs you to
  run was a silent no-op. The lock now guards only the interactive launch; a blocked op exits nonzero.
- **A denied access gate exits nonzero** instead of 0, so automation can tell blocked from succeeded.
- **Batch flashing extracts per-board ZIP bundles** (e.g. GhostESP) instead of writing the raw `.zip` and
  reporting success on a non-booting board (parity with the main flash path).

### Known issue
- The Windows `win_acl` per-user hardening no-op (see 1.6.2) is unchanged, pending an owner decision.

## [1.6.2] — 2026-07-06

A security-hardening release from a second, deeper adversarial review. It removes a class of *false
security* on the tool itself (the GUI Dead Man's Switch flow claimed protection it never applied), closes
several remote/supply-chain and OS-verification holes, and adds a per-port guard that stops a
double-flash from bricking a board. No firmware changes; no breaking changes to profiles or the CLI.

### Security
- **Dead Man's Switch GUI flow now fails safe.** The Qt and Tk "Enable Dead Man's Switch" flows opened
setup, provisioned a host-side guardcfg bundle, then flashed **plain** firmware and reported success —
the board ran vanilla firmware with no boot gate while the user believed it was gated. Both GUIs now
abort with clear next steps (use `cyber-controller --deadman-setup`) instead of flashing an unprotected
board that looks protected. (Actually wiring the gate flash into the GUI touches the eFuse/brick path and
remains CLI-only for now.)
- **Web remote can't lock the owner out or trigger a wipe.** A request with no credentials no longer
counts toward the shared brute-force lockout (an unauthenticated cross-site GET could previously lock the
local gate), and the network surface can never fire the physical duress wipe.
- **Socket.IO client is vendored, not loaded from a CDN.** It was pulled from cdnjs with no SRI; a
compromised CDN could run arbitrary JS in the authenticated remote. It's now served same-origin (tighter
CSP, works offline).
- **OS image verification hardened.** An unpinned GPG key no longer rubber-stamps any signature (Arch),
and Parrot's inline-clearsigned hashes file is now actually verified against the pinned key before its
SHA-256 is trusted (closes a MITM/compromised-mirror gap).
- **Bounded anti-replay after a crash.** The node receive-window head is now persisted as the session
runs (not only at clean detach), so a crash can't roll it back and re-open captured frames to replay.

### Fixed
- **Double-flash brick guard.** A second flash/backup/erase on a port already mid-operation is refused
(two esptool processes on one UART can brick a board); different ports still run in parallel. The web API
returns 409.
- **Packaged TUI no longer crashes on launch** — its stylesheet is bundled + resolved frozen-safe (third
instance of the frozen-resource class; the AST bundle-manifest guard now covers it).
- **Node gateway sharing.** Detaching/closing one wireless node no longer force-closes the shared dongle
(which killed every other node + the Devices tab); a NodeLink now detaches cleanly and leaks no callbacks.
- **Wildcard web bind** (`--host 0.0.0.0`) now adds the machine's real LAN origin, so LAN Socket.IO
handshakes aren't silently rejected.
- **Malformed profile handling** in the TUI and Tk flash paths (parity with the Qt guard) — a bad profile
is reported, not a crash or a silent no-op.

### Known issue
- **Windows per-user file hardening (`win_acl`) is a no-op when the current-user SID can't be resolved**,
leaving secrets (web session key, vault, settings) on their inherited ACL — potentially readable by other
local accounts on a shared machine. Flagged for an owner decision before changing that fenced-off code;
tracked with runtime evidence. Not a regression (present in prior releases). Single-user machines are
unaffected in practice.

## [1.6.1] — 2026-07-06

A stability, correctness, and privacy release. Real flashing bugs surfaced by hardware testing and a full
adversarial code review of the app, plus two PII fixes in the bug reporter.

### Added
- **CYD board detection** — a "Detect board" button on the Flash tab flashes a tiny probe to read the
  panel's display-controller ID, touch type, and peripherals, then auto-selects the matching firmware
  variant. A CYD (ESP32-2432S028) no longer ends up with a blank/white screen from a wrong-driver build.
- **Report a Bug** (Help menu) — assembles a redacted diagnostics bundle (version, platform, recent logs,
  your note) that you can save, copy, or open as a prefilled GitHub issue to send back for fixing.
- **Hold-BOOT recovery hint** — when esptool can't enter the bootloader ("Wrong boot mode detected",
  "Failed to connect"), the log now says exactly what to do: hold BOOT, tap EN/RST, lower the flash baud,
  use a data-capable cable. Fires for flash, backup, and erase.
- **8 MB Dead Man's Switch guardian** partition table.

### Fixed
- **M5Stick and other multi-chip boards couldn't flash** — profiles whose boards span several chips
  (Marauder, ESP32-DIV, GhostESP, AirTag-scanner, M5Stick) pinned the chip to the first board, so an
  ESP32-S3/-C5 board got the wrong `--chip` (esptool aborted) and its build wasn't even offered. They now
  auto-detect the chip and the picker lists every board's build.
- **Operate tab didn't send commands** — the terminal now stamps the connected device's firmware on
  connect so commands route to the right backend.
- **Web Remote UI was non-functional in the installed build** — its templates/static were resolved via
  `__file__` and never bundled, so every page returned HTTP 500 once installed (dev was fine). Now bundled
  and resolved frozen-safe.
- **Vault download, Backup, and Erase blocked or could crash the window** — moved off the GUI thread onto
  worker threads (the vault download had been mutating widgets from a raw thread — a frozen-build crash).
- **A bad firmware profile could crash the app** — the flash path now guards profile parsing, and browsing
  to an unparseable `.json` no longer registers it as a flashable selection.
- **Dead Man's Switch "LOW = armed" always failed provisioning** — the fail-safe pull is now derived from
  the armed level.
- **Two mislabeled boards removed** — a Meshtastic nRF52840 board listed under esp32c6, and an ESP32-DIV
  classic-ESP32 board that could only ever be flashed wrong-chip S3 firmware.
- **CYD variant selection could pick the wrong display driver** — variant matching is now token-bounded, so
  a `cyd_2432S028` fragment can't match the `_2usb` (ST7789) build regardless of release asset order.
- **A shared serial port reused at a different baud** (e.g. a GPS opened at 115200 then reused at 9600) now
  warns instead of silently producing a "No Fix".
- Installed-build breakage from the 1.6.0 line: bundled starter macros, the Flock map tab, and broadcast
  gating now ship correctly.

### Changed
- Marauder ESP32-C5's "no upstream support files" abort now reads as a permanent gap for that chip, not a
  transient "fix the connection and retry" error.
- The Nodes banner no longer claims "no firmware yet" — the relay/node sketches ship (source-only) in
  `firmware/`; only live over-the-air attach/detach from that view is still pending.

### Security
- **Closed two PII leaks in bug reports.** BSSIDs (directly WiGLE-geolocatable → your physical location)
  are now redacted, and SSIDs are kept out of the captured log ring at the source. The prefilled GitHub
  issue **title** is redacted too, not just the body.

## [1.6.0] — 2026-07-05

The 1.6.0 line, complete. Everything the beta pointed at has landed: a live Flock driving map, concurrent
multi-device wardriving, recommended-hardware guidance, and real relay/node wireless firmware.

### Added
- **Multi-device wardriving** — a new Multi-Wardrive tab drives several boards at once from one shared GPS
  into a single merged WiGLE CSV, with per-board AP counts and a running total. Each board is sent its own
  firmware's native scan command (Marauder, GhostESP, Flock-You, and so on), and captures route through the
  shared DeviceManager, so a board already open in the Devices tab is shared rather than double-opened
  (which on Windows would fail with Access Denied).
- **Live Flock driving map** — the Flock Map tab now records a live drive: located ALPR cameras drop onto the
  map as they're found, each checkpointed to disk for crash-safety. The recorder keeps running while the tab
  is hidden; only the repaint pauses, and the map catches up when you return to it.
- **Wireless relay + node firmware** — real ESP32 sketches for both roles: a crypto-free USB↔ESP-NOW relay
  and an AES-256-GCM sensor node, with a byte-exact wire spec in `firmware/PROTOCOL.md`. The node's on-device
  crypto and anti-replay are contract-tested against the host (the sketches are source-only for now; compile
  and flash them yourself).
- **Parrot OS** in the Software-OS flasher — the version is auto-resolved from the official ISO index with
  SHA-256 + OpenPGP verification and a pinned offline fallback. The OS flasher now covers Kali, Tails, Parrot,
  and Arch.
- **Recommended-specs guide** (`docs/RECOMMENDED-SPECS.md`) — which of the four frontends fits which host,
  from a desktop GUI down to a headless Raspberry Pi.
- **Supported-boards tooltip** on the Firmware tab, and a **board-compatibility hint** that tints a firmware
  green or red against the connected board's chip (advisory only — it never blocks a flash).
- **Companion mobile app** is now its own project (Bluetooth + WiFi now, cellular later).

### Changed
- The wireless **Nodes** feature is real now: the relay/node firmware the host-side groundwork was waiting on
  ships in `firmware/`.
- Tightened the README voice. No facts, versions, flags, or commands changed.

### Fixed
- `from_checkpoint` no longer raises on a malformed checkpoint file — it returns an empty session, as documented.

## [1.6.0-beta.1] — 2026-07-05

First public preview of the 1.6.0 line. More is landing before the full 1.6.0 — a live Flock driving
map, multi-device wardriving, recommended-hardware guidance, and the wireless relay/node firmware. This
beta ships the pieces that are done and tested.

### Added
- **Parrot OS** in the Software-OS flasher — the version is auto-resolved from the official ISO index with
  SHA-256 + OpenPGP verification and a pinned offline fallback. The OS flasher now covers Kali, Tails, Parrot, and Arch.
- **Supported-boards tooltip** on the Firmware tab — hover a firmware to see the boards and chips it targets.
- **Board-compatibility hint** — a firmware is tinted green or red against the connected board's chip. It's
  advisory only: it never blocks a flash and never shows a false red on a guessed chip.
- **Flock Map tab** — the located-ALPR-camera map is a tab in the Operate surface now, next to Wardrive
  (it used to open as a separate Tools window).
- **Crash-safe Flock drives** — a scan checkpoints its located cameras to disk as it runs and can resume from
  that file after a restart. Two drives (or a synced set) merge by the strongest-signal rule.
- **Companion mobile app** is now its own project (Bluetooth + WiFi now, cellular later).

### Changed
- The wireless **Nodes** view is labeled a demo/placeholder — the host-side groundwork is in; the relay/node
  firmware is still to come.
- Tightened the README voice. No facts, versions, flags, or commands changed.

### Fixed
- `from_checkpoint` no longer raises on a malformed checkpoint file — it returns an empty session, as documented.

## [1.5.1] — 2026-07-03

Ships the accumulated work since 1.5.0.

### Security
- Web remote is covered by the persistent brute-force lockout (SEC-A1); the access-gate failure counter is a lost-update-safe read-modify-write (SEC-A2).
- The access gate fails CLOSED on a corrupt config instead of open (SEC-C2); an enabled secure container is never silently downgraded to plaintext (SEC-B1); its key is minted once, not re-minted on status/read (SEC-B2).
- The ACL is granted by the process token SID, not a spoofable env name (SEC-D1); a generated web password is kept out of logs, a tampered vault fails cleanly, and ACL-harden failures are loud. Corrected an audit-trail tamper-evidence over-claim (SEC-C1).

### Added
- **In-app auto-updater** — a silent startup check against GitHub Releases, prompt-to-update with a "don't show again" option (re-prompting when two or more versions behind), graceful offline handling, and a Help-menu / command-palette "Check for Updates".
- **Three firmware profiles** (29 total): ESP8266 as a first-class chip + `esp8266_deauther`, M5Stick NEMO, and RogueMaster Flipper CFW.
- **`nrf_dfu`** flash backend (Nordic nRF52 DFU `.zip`).
- Per-device capability map, a connect-time firmware health probe (CC-7), and firmware autodetect from the handshake reply.

### Changed
- **Grouped tab UI:** the tab strip folds into six top-level surfaces — Flash (Firmware + Software-OS), Connect (Devices + Health), Operate (Targets + Broadcast + Macros + Wardrive), Network (graph + Cross-Comm), Settings — and How-To moves into the Help menu (CC-6).
- Cross-comm hardening: a `CrossCommHub` spine with driver-type dispatch, a Meshtastic Stream-API frame codec, and a binary serial write path.
- Release CI publishes one consolidated `SHA256SUMS.txt` and runs a VirusTotal scan on every release.

### Fixed
- Flash/serial robustness: kill the flash child on Ctrl-C and don't reuse a corrupt cached zip; serialize per-port connection builds (no concurrent-open leak); frame serial lines on CR/CRLF/LF (CR-only firmware was never framed); a loud error on an undetected flash size instead of a silent 4 MB truncation; fail loud instead of flashing a dead board or claiming an unrun verify; hardened the Windows raw-disk write path; robust reconnect + firmware probe; honor a pinned firmware version.
- The esptool version guard now flags 4.0–4.6 (the pin is >=4.7); benign RTL8720 "sync" chatter no longer reads as a flash failure.

## [1.5.0] — 2026-07-01

### Fixed
- **Access gate (data-loss).** A key-only gate under the default policy no longer burns every unlock attempt instantly (which tripped the lockout and any opt-in duress vault-wipe on a normal boot); and an exclusive policy whose factor isn’t configured is now rejected (no self-lockout).
- **Flashing.** Flipper (Momentum/Unleashed) flashes the real downloaded package instead of launching a bare qFlipper and reporting false success; backup detects the real flash size instead of truncating >4 MB boards; the batch flasher enforces the SHA-256 pin; the ESP32-C5 2nd-stage bootloader offset is 0x2000 via an esptool-faithful SSOT helper.
- **Safety.** Network-tab device-node commands are gated + no longer send unfilled `<…>` templates raw; BlueJammer control-map loader defaults `validated=False` (an unmarked map can’t send frames); RF-transmit commands (Tesla opener, SubGHz tx) now hit the lab-only confirmation.
- **Cross-device.** MAC-keyed target scan index is refreshed on re-observation (index actions stop firing at the wrong AP); the AutoRouter cooldown is atomic + bounded; the Dead-Man-Switch auto-auth reply goes to the device that emitted the prompt; per-device line terminator is re-stamped on send; on_line/ingest callbacks no longer stack on reconnect (both serial UIs); `TargetIngestor.attach` is idempotent; a device serial ERROR now reflects into `Device.connected`; targets-tab actions resolve against the pooled target.
- **Integrity.** Settings Save no longer wipes interface mode + loadout; the audit trail survives a torn JSONL line instead of disabling all persistence; Flipper SubGHz captures keep their protocol + key; the Dead-Man-Switch provisioner fails loud on an unknown flash_size instead of silently baking the 4 MB layout; the app version is single-sourced (`src/__init__` re-exports `src/version`).

### Changed
- **LxveAce violet identity theme.** The interactive/brand accent is now LxveAce violet (`#a371f7`), retiring the generic acid-green across all four front-ends (Qt/Tk/TUI/Web); functional green is kept for connected/online status and live serial output. Guarded by `test_no_acid_green`.

### Added
- **dfu-util + UF2 flash backends** *(experimental — HW-validation pending)* behind `FlashEngine._backends` for RP2040 / Pi Pico (DFU) and UF2 mass-storage bootloaders; download-or-local resolve, never fake success; unit-tested. See `docs/ROADMAP-FUTURE.md`.
- **`docs/ROADMAP-FUTURE.md`** — the forward roadmap (more backends/hardware, mission planner, RF/recon).
- **Regression guards:** bundle-manifest test (`build.py` data ↔ runtime `resource_path()`), firmware-profile count drift-lock (README ↔ shipped JSONs), and a `resource_path` frozen-bundle check.
- **Per-device capability map (network integration).** Each firmware/board now declares what it can do
  (`wifi`/`ble`/`subghz`/`nfc`/`ir`/`rfid`/`gps`/`lora`/`nrf24`/`deauth`/`badusb`…) via `BaseProtocol.capabilities`,
  surfaced as a capability chip line in the Devices tab so every connected device reads as a node in the
  network, and exposed via `protocols.capabilities_for(name)` for Broadcast/AutoRouter. `src/protocols/*.py` +
  `src/ui/qt/device_tab.py`; +tests.
- **Argument-entry form for placeholder commands.** Sending a command that contains `<...>` placeholders
  (e.g. `scanap -c <ch>`, `select -a <idx>`, `led -r <v> -g <v> -b <v>`) now pops a small parameter form —
  one field per token, occurrence-ordered so repeated tokens get distinct values — instead of transmitting
  the literal `<ch>`. Values are sanitized (control chars + angle brackets stripped, 64-char cap); hooks the
  single send chokepoint so typed and palette-selected commands are both covered. `src/ui/qt/device_tab.py`; +5 tests.
- **`{index}` target-action substitution + source-restriction.** Target actions that select by scan index
  (e.g. Marauder's `select -a {index}`) previously sent the literal text `{index}`. The resolver now
  substitutes a real per-device scan index when known, and — because a scan index is only valid for the
  device that produced it — **source-restricts** index actions to the discovering device and **drops** them
  when no index is known (rather than firing at the wrong AP). `src/core/action_resolver.py` +
  `src/core/target_ingest.py`; +4 tests.
- **BW16 right-click deauth-by-index.** The BW16 Vampire scan prints index + SSID but no BSSID, so those APs
  used to be dropped from the Target Pool. They now enter under a synthetic **source-tagged** key carrying the
  scan index, and BW16 gets a `Deauth (this index)` action (`AT+DEAUTHIDX={index}`) the resolver offers only
  on the BW16 that scanned it. Completes the one fully-verified firmware. `src/protocols/bw16.py` +
  `src/core/target_ingest.py`; +3 tests.
- **Removed phantom target actions** that referenced commands the firmware never exposes (presented as
  working but doing nothing): HaleHound `analyze`, Meshtastic `relay` (its serial link is protobuf-framed, not
  text), and Flipper `bt spam` (no stock-CLI command) — in both `TARGET_ACTIONS` and the broadcast map.
  `src/protocols/{halehound,meshtastic,flipper}.py`; +3 tests.
- **Per-firmware command terminator (Flipper CR fix).** The serial line terminator now follows the selected
  firmware's protocol — most firmwares submit a line on LF, but the **Flipper Zero** CLI shell only submits on
  CR (`\r`), so CC's commands previously never executed on a Flipper. `BaseProtocol.line_ending` (Flipper =
  `\r`, everyone else `\n`) is applied to the live connection on connect / firmware-change.
  `src/core/serial_handler.py` + `src/protocols/{base,flipper}.py` + `src/ui/qt/device_tab.py`; +3 tests.
- **Command-correctness pass (source-verified against firmware).** Fixed command tokens that didn't match the
  real firmware CLIs: **Marauder** v1.12.3 (`scanap`→`scanall`, `channel -s`, `sniffbt`/`sniffskim`,
  `blespam -t sourapple/windows`, `gpsdata`/`nmea`, `settings -s`, `led -s`) **plus a stateful multi-line AP
  parser** so the Targets tab actually populates (the firmware prints ESSID/BSSID/RSSI on separate lines);
  **GhostESP** (`attack -d`, `beaconspam -r/-rr`, `startportal`/`stopportal`, `capture -eapol/-stop`, `startwd`,
  `list -a`, `chipinfo`); **Bruce** (`ir rx`/`tx`, `subghz rx/tx/tx_from_file`, `badusb run_from_file`; reads its
  `COMMAND:`/`[CLI] Result:` shell; fabricated wifi/ble/nfc serial verbs removed — those run via menu/loader).
  Device-View skins updated to the corrected commands. `src/protocols/{marauder,ghost_esp,bruce}.py` +
  `src/ui/qt/device_view.py`; +tests. (esp32div/halehound kept pending hardware confirmation; Meshtastic is protobuf.)
- **Loadout — tailor the GUI to what you actually use.** On first run, pick the firmwares + hardware you use
  (or **Full Stack** = everything) and Cyber Controller hides the tabs you won't need (de-bloat); change it
  anytime via **View ▸ Loadout**. Orthogonal to Simple/Pro (which controls depth). Core tabs (Flash, Devices,
  Health, Macros, Settings, How-To) always stay; **fail-open** — Full Stack / unconfigured / empty shows
  everything, so nothing is ever stranded. `src/config/loadout.py` + `src/ui/qt/loadout_dialog.py`; +15 tests.
- **BlueJammer remote-controller framework.** `src/core/bluejammer_control.py` — a transport-abstracted
  controller for the BlueJammer's modes: **UART-first (the inter-board wire — no Wi-Fi AP/IP needed)** with
  the web UI (`http://192.168.1.1`) as an option. STOP (Idle) is the primary, ungated action; arming a
  jamming mode requires explicit confirmation (GUI gates it behind an RF-shielded-enclosure attestation,
  per 47 U.S.C. §333). **Fail-safe:** refuses to send until a hardware-captured/validated control map exists
  (a STOP that silently does nothing is the failure mode we avoid). The exact UART frames / HTTP endpoints
  are closed-source and captured on hardware (see the reverse-engineering plan). +10 tests.
- **BlueJammer control / STOP panel — full in-app remote control.** When a BlueJammer-V2 is the active
  firmware, the Devices tab shows a prominent control panel wired to the `BlueJammerController`: a large,
  always-available **STOP** (set Idle); an **RF-shielded-enclosure attestation** that gates the **arm-mode**
  buttons (Bluetooth / BLE / WiFi / RC-Drone), each behind a per-press confirm; a **Load control map…**
  action; an **Open control web UI** launcher; and a live status line. Proper remote control is the safety
  mechanism — arm and, critically, *instantly STOP* without standing next to an active transmitter.
  **Fail-safe:** the control frames are closed-source, so the app never sends guessed frames — live
  transmission activates once a **validated control map captured from your own device** is loaded (UART
  frames or web-UI calls); STOP / web UI / power remain available meanwhile. Illegal to operate outside an
  authorized RF-shielded lab (47 U.S.C. §333). `src/ui/qt/device_tab.py`; +7 panel tests.
- **Device View — Marauder / GhostESP / ESP32-DIV skins (now drives the device).** A new
  **Tools → Device View** opens an on-screen reconstruction of a firmware's on-board TFT menu (header,
  breadcrumb, selection highlight, submenus) at the device's real 240×320 resolution, scaled into a
  resizable window. **Clicking a menu item now sends that firmware's real serial command to the connected
  device** — but only when the active device's firmware matches the skin, and routed through the same safety
  prompts as the Devices tab; otherwise it stays a labelled **preview** (so it never sends one firmware's
  commands to another, and never implies control it doesn't have). Every leaf is grounded against the real
  protocol command set by tests. Honest framing: a faithful *reconstruction*, not a pixel mirror — only the
  Flipper's RPC can be a true mirror (a later phase). `src/ui/qt/device_view.py`; +12 tests. (Plan P2/P3.)
- **Detachable / pop-out tabs.** Any tab can pop out into its own resizable, top-level window — drag it to a
  second monitor, resize it freely — and re-dock seamlessly back onto the tab strip. Detach via the tab-strip
  **⇱** corner button, **double-clicking** a tab, the tab right-click **"Pop out"**, or **Ctrl+Shift+D**;
  re-dock via the **⤓ Re-dock** button or simply **closing** the pop-out (closing re-docks by default, so a
  working panel is never lost). The set of popped-out tabs + their window geometry persists across sessions.
  Foundation for the planned per-firmware "Device View." (`src/ui/qt/detachable_tabs.py`; +10 tests.)
- **Cyberdeck-aware scaling.** The app now adapts to the screen instead of assuming a desktop: the hard
  900×600 minimum is **relaxed to fit small deck panels** (down to ~480×320 on an 800×480 / 1024×600 screen),
  the launch size is clamped to the screen, **high-DPI fractional scaling** is enabled app-wide, and the
  streamlined **Simple interface auto-engages on small/touch screens** (your explicit Simple/Pro choice still
  wins). (`src/ui/qt/screen.py`; +11 tests.)

### Fixed
- **Dead Man's Switch serial auth detection.** The host-side prompt/result patterns now match the boot-gate's
  *actual* serial strings (`suicide-gate: enter …` / `… wrong. attempts left: N` / `… locked for Ns.`), and
  benign `SM>{…}` status JSON is explicitly never treated as an auth prompt (safe to poll). (`deadman_auth.py`;
  +6 tests.) Full suite **488 passed / 2 skipped**.

### Distribution / trust
- **App icon.** Generated `assets/icon.ico` (multi-resolution 16–256 px) so the Windows `.exe` is branded in
  Explorer/taskbar (build.py already passed `--icon` but the file was missing, so it was a no-op until now).
- **Real Windows installer.** New `installer/cyber-controller.iss` (Inno Setup) packages a `--onedir` build
  (added to `build.py`) and registers the standard Add/Remove Programs keys, so the app appears under
  **Settings → Apps → Installed apps** with an uninstaller instead of being a loose `.exe`. CI builds it on
  release (best-effort; unsigned — see below).
- **Published checksums.** The release workflow now emits a `.sha256` next to each binary (Windows/Linux/macOS)
  so downloads are verifiable.
- **Trust guide.** New `docs/WINDOWS-SECURITY.md` explains the SmartScreen "unrecognized app" prompt (and how
  to *More info → Run anyway*), why an unsigned PyInstaller build draws AV false positives, and three ways to
  verify the download yourself (SHA-256, VirusTotal, build from source). Code-signing remains the cert-gated
  follow-up that retires the prompt for good.

## [1.4.0] — 2026-06-29

### Added
- **Smart installation / version-aware startup.** Cyber Controller now reconciles the persistent config
  in `~/.cyber-controller` against the running version on launch (`src/core/install.py`):
  - **Upgrade** (config from an older version) → carried forward silently (settings already deep-merge
    onto defaults; the AES-GCM vault carries its own format version) and the version marker is advanced.
  - **Downgrade** (config from a *newer* version — the "paths collide / overwrite old install" case) →
    the GUI prompts: **Keep & Continue**, or **Back up & Start Fresh** (the old config is *moved aside,
    never deleted*, so it's restorable).
  - **Fresh / same** → recorded, proceed. A `.installed_version` marker is written so future launches can
    classify correctly. Safe + silent for headless/CLI use.
- Single source of truth for the app version (`src/version.py`), used by the window title + the installer
  logic. +9 tests.

## [1.3.3] — 2026-06-29

### Added
- **Animated startup for the PyQt5 desktop.** A frameless animated loading screen (logo + indeterminate
  progress sweep + status text) now appears while the dashboard builds, then cross-fades to the main
  window. Motion follows the project's motion-design tokens (fade-in OutQuart ~320ms, linear looping
  progress, fade-out OutCubic ~260ms). Honors reduced-motion (`interface.reduced_motion` /
  `CC_REDUCED_MOTION`). Reserved for the full GUI only — the lightweight Tk/TUI/web UIs are unanimated.

## [1.3.2] — 2026-06-29

Launcher / interface-selection fixes.

### Fixed
- **The "Select Interface" launcher was hidden by the splash and missing a mode.** When you launch
  without `--ui` (i.e. double-click the exe), the app shows a chooser for which interface to run — but
  (a) the always-on-top startup splash (added in 1.3.1) was covering it, so it looked like the app never
  asked, and (b) it only listed **3** of the **4** advertised UIs. Now: the splash is closed *before* the
  launcher appears, and the launcher offers all four — **Full GUI (PyQt5) · Lightweight (Tkinter) ·
  Terminal UI (Textual) · Web Remote (Flask+SocketIO)** — matching the website. Dialog resized to fit.
- **Web Remote was unusable from a packaged build** — a `--windowed` exe has no console to print the
  server URL, so picking "Web Remote" appeared to do nothing. It now **opens your default browser** at
  the server URL automatically once the server is warming up.

## [1.3.1] — 2026-06-29

Installer-UX + reliability fixes.

### Fixed
- **"Installation error" on the Windows .exe was a slow, feedback-less startup.** The `--onefile
  --windowed` build self-extracts ~80 MB to a temp dir on launch (~15 s on a cold first run) with **no
  visual feedback**, so users saw nothing for 15+ seconds and assumed it failed. Added a **PyInstaller
  splash screen** (the cc logo) that appears within ~1–2 s of launch and is closed by `launch_qt()` once
  the main window is ready (`pyi_splash.close()`). Verified end-to-end: splash visible at t≈2 s →
  clean hand-off to the GUI. (Full diagnosis captured in the internal engineering notes.)
- **MinigotchiV3 profile crashed the Flash UI.** Its resolver pointed at `dj1ch/minigotchi-V3`, which
  upstream renamed to `dj1ch/minigotchi-ESP32` (the old path now 404s) and — unlike the other
  source-only profiles — had no `on_error` fallback, so resolving it raised an uncaught HTTP 404.
  Corrected the repo + added the graceful `source_only_empty` fallback. `halehound` given the same
  defensive fallback (builds from source; no `.bin` release assets).

### Notes
- Recommended follow-ups for the installer (documented, not all doable in CI here): a `--onedir` build +
  real installer (Inno Setup) for near-instant startup, bundle slimming, and code-signing (unsigned exes
  can trip SmartScreen/AV — the most likely cause of a genuine failure on an end user's machine).

## [1.3.0] — 2026-06-29

Security-hardening + UX release.

### Added
- **Secure container (opt-in).** App-internal saves (e.g. recorded command sessions) are encrypted at
  rest with AES-256-GCM in a gate-keyed container (`~/.cyber-controller/secure`) that is **sealed and
  unreadable while the access gate is locked** — the key lives only inside the unlocked vault.
  Ciphertext-only writes (no transient plaintext), GCM-authenticated (tamper fails closed), 0600 + ACL.
  Toggle in **Settings → "Secure Container"**; integrated with the duress wipe.
- **Brute-force lockout** on the access gate — persistent failed-attempt counter (survives restart) +
  exponential-backoff cooldown; constant-time password compare.
- **Duress self-wipe (opt-in, off by default).** After a configurable number of consecutive failed
  unlocks, securely wipe the app's own footprint (vault/keys/config/container). Honest scope documented:
  defeats casual/seizure access, not a forensic lab on wear-leveled SSDs.
- **Dual-depth Simple/Pro interface.** A streamlined Simple view (fewer controls per tab) and the full
  Pro view (default, zero penalty). Toggle via **View → Interface Mode**, the status-bar badge, or
  **Ctrl+M**; persists. Streamlines Flash/Settings/Health/Software/Macro/Cross-Comm.
- **4 new firmware profiles** (drop-in JSON): **T-REX** (LilyGo T-Deck), **MCLite** (MeshCore),
  **ESP32 Bit Pirate**, **Hydra32 / ESP32-Deauther** (multi-file, SHA-256-pinned; offsets verified from
  the upstream partitions.csv). Registry → 22 profiles (21 flashable firmware + custom loader).
- **esptool range guard** — a clear "install esptool>=4.7,<6" message if an out-of-range esptool (v6+)
  is installed, instead of a cryptic argparse failure mid-flash.

### Changed / Hardened
- **Boot/startup-bypass resistance.** Modifying an already-configured access gate (clear / change
  password / change policy / add key) now requires **passing the gate first** — no pre-auth reset.
- Locked-state data-leak audit + offline-posture audit completed (see `SECURITY.md`); no telemetry,
  all network is user-initiated firmware/OS download.

### Internal
- New test suites: secure container + macro-container integration, access-gate mutation auth, esptool
  version guard, dual-depth UI, Hydra32 profile. Full suite **452 passed / 2 skipped**; flash-argv
  golden regenerated (purely additive).

## [1.2.1] — 2026-06-27

### Added
- **In-app access-gate setup.** The admin password / physical USB key / unlock policy can now be
  configured from the GUI — **Settings → "Access Gate (Security)" → "Set up access gate…"** — not just
  the CLI. Backed by the same salted-scrypt verifiers + gate-keyed encrypted vault; changes apply on
  the next launch. Verified end-to-end (set/clear password, policy change, vault provisioned and
  ciphertext-at-rest, wrong password rejected).

## [1.2.0] — 2026-06-27

Major feature + reliability release.

### Fixed
- **Windows one-click `.exe` launched to a silent crash.** CI never bundled the Qt QSS theme and
  `apply_theme()` read it with no fallback under `--windowed`, so the frozen GUI died at startup. Builds
  now go through `build.py` (single source of truth) which bundles the theme + every resource, and theme
  loading degrades gracefully. Verified: the built `.exe` launches the full GUI past startup.

### Added
- **Software-OS flashing** — a *Software OS* tab + `--list-os` / `--flash-os` CLI to write verified
  bootable operating systems (**Kali, Tails, Arch**) to USB, separate from firmware. Latest version
  auto-resolved online; the bundled catalog works fully offline; integrity-verified (SHA-256 + OpenPGP).
- **Wardriving** (lawful, owner-authorized) — a *Wardrive* tab + core that logs GPS-tagged Wi-Fi as
  **WiGLE CSV** (`WigleWifi-1.6`) from a GPS NMEA port + the ESP32 Marauder scan output.
- **Physical-key access gate + encrypted vault** — gate the app behind an admin password and/or a
  physical USB key; vault data stays encrypted at rest until unlocked (fail-closed; no boot-sequence
  bypass).
- **Tails flashing** (`--flash-tails`) and **Dead Man's Switch** setup, working in the frozen build.
- **How-To tab** — an in-app guide covering every tab/feature; tooltips across the new UI.
- A weekly CI job that refreshes the bundled OS catalog versions/checksums.

### Internal
- New test suites for the OS catalog (14), wardriving (8), and the new Qt tabs; full suite green.

## [1.1.0] — 2026-06-12

A large feature + hardening release. Every change below was validated against the test suite
(green) and, where noted, on real hardware. New capabilities are backward-compatible.

### Added
- **Unified Action Broadcast** — one intent verb (e.g. *Find APs*, *BLE Scan*, *Deauth All*) fans
  out to **every connected radio at once**, each in its own native command, with results converging
  back into the shared Target Pool. New `src/core/broadcast.py` engine, a `BROADCAST_CAPABILITIES`
  map on all protocols, and a Broadcast tab in the GUI. *Live-validated:* "Find APs" → BW16
  `AT+SCAN` (dual-band) + GhostESP `scanap` (94 APs) simultaneously.
- **GhostESP `.zip` bundle flashing** — GhostESP ships per-board zips containing a `merged.bin`;
  the profile now extracts and flashes that at `0x0`. Path-safe `download_and_extract()` with cache
  reuse. *Validated end-to-end on a classic ESP32.*
- **Meshtastic per-chip zip flashing** — handles the 128 MB per-chip archives (factory bin @0x0,
  bleota @0x260000, littlefs @0x300000) with a curated board list. *Validated on a Heltec LoRa V3.*
- **BW16 / RTL8720DN (Realtek AmebaD) flash backend** — first-class support for a non-ESP32 radio
  (dual-band 2.4/5 GHz), including the `bw16` serial protocol. *Validated on real hardware.*

### Security (full audit — all 10 findings closed)
- **H-1** Pin + verify the SHA-256 of the BW16/RTL8720 firmware bundle before flashing.
- **H-2** Never silently serve LAN traffic on the Werkzeug dev server — require a reverse proxy or
  an explicit `CC_WEB_ALLOW_DEV_SERVER=1` opt-in for a non-local bind.
- **M-1** `subscribe_serial` is idempotent + rate-limited and `remove_line_callback` was added,
  killing a callback-leak / serial-emit amplification DoS.
- **M-2** The firmware vault's GitHub *API* GETs now go through the same SSRF allowlist (manual
  redirect re-validation) as the binary download path.
- **M-3** Session + CSRF token are rotated on successful login (session-fixation defense).
- **M-4** `install_rayhunter` validates `admin_ip` as a literal IP before it reaches argv / a URL.
- **L-1** Explicit owner+SYSTEM **NTFS ACLs** on `~/.cyber-controller` and the web secret key /
  encrypted vault / settings (POSIX `0600` is a no-op on Windows).
- **L-2** The hash-chained **audit trail is now durable** — append-only, owner-only, loaded and
  verified on startup; the web remote warns if it has no audit sink.
- **L-3** Removed the misleading no-op password "zeroization" in the dead-man auth relay.
- **L-4** Strict **CSP nonce** for `script-src` (dropped `'unsafe-inline'`); all inline scripts are
  nonce-tagged and former inline `on*=` handlers moved to `addEventListener`.

### Changed / Performance
- UI perf (no visual or behavior change): HealthTab reads the cached system-health snapshot instead
  of a 100 ms GUI-thread `psutil` block; per-class memoization of protocol command lists; bounded
  terminal/log memory via `maximumBlockCount`.
- Bruce firmware repo updated to the canonical **BruceDevices/firmware** (formerly `pr3y/Bruce`).
- Firmware profile count corrected to the verified **19**.

## [1.0.0] — 2026-06-11

First official release: flash-core overhaul, web remote + security baseline, and the initial
firmware/profile set.

[1.1.0]: https://github.com/LxveAce/cyber-controller/releases/tag/v1.1.0
[1.0.0]: https://github.com/LxveAce/cyber-controller/releases/tag/v1.0.0
