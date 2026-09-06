# Cyber Controller — How-To

Cyber Controller is an all-in-one controller, flasher, logger, and pentest GUI for ESP32 security
gear and cyberdecks. No account is required. Firmware and OS downloads need internet; the bundled
catalogs contain definitions and download links, not an image library. Offline OS flashing needs a
compatible local image and any verification files needed for it. **Lawful, owner-authorized use only.**

Hover any button or field to see a tooltip explaining what it does. The tabs across the top are:

## Flash (firmware)
Write firmware to a connected board (ESP32 Marauder, GhostESP, Bruce, etc.).
1. Plug in the board; pick its **Port** (Refresh re-scans).
2. Pick a **Firmware Profile** and, if your board has a screen (CYD/M5/…), the matching **Board /
   variant** (Auto guesses per-chip and can be wrong for display boards).
3. **Flash**. The current dashboard resolves the configured upstream source and downloads the image.
4. Optional **Dead Man's Switch**: enable it to weave the anti-forensic wipe into the flash (a setup
   dialog opens first).

**Offline Vault:** the download/cache controls are not connected to the current single-window
interface yet. The legacy Vault supports selected merged images; it cannot cache firmware that needs
separate bootloader, partition and application files. Setting a Vault directory does not enable
offline flashing in the dashboard.

## Software OS (flash an OS to USB)
Write a bootable operating system to a **USB stick** (separate from board firmware) using the CLI.

1. Run `cyber-controller --list-os` to see the catalog, then choose an ID for `--flash-os <id>` —
   **Tails**, **Kali**, **Parrot** or **Arch**, for example.
2. By default, the app tries to resolve the current version online. `--offline` uses the saved
   catalog version instead; it **does not disable image downloads**. The old Software tab calls this
   “Use bundled version (offline),” but only the version metadata is bundled.
3. For a local image, pass `--os-image <path>` and, where applicable, `--os-sig <path>` for its detached
   signature. The image must match the selected catalog entry/version. Without a local image, the
   app still needs to download it.
4. Pick the **target USB** when prompted (only removable drives are listed). Confirm the target
   carefully: the whole drive is erased.
5. Review the verification output. Depending on the available files and keys, the app may verify a
   signature, verify only a checksum, or report the image as unverified. Check the image against its
   official source before starting; a checksum match alone is not signature verification.

## Devices
Connect to and control attached radios/boards: open a serial console, send commands, and watch live
output. Targets discovered here are shared across the app via the Target Pool.

## BlueJammer-V2 (lab-only — illegal to operate)
When a **BlueJammer-V2** is the active firmware, the Devices tab shows a dedicated control/STOP panel.
**Operating an RF jammer is illegal** outside an authorized RF-shielded enclosure (47 U.S.C. §333) — Cyber
Controller's job is **flash + STOP/containment**, and it ships **no jammer control frames**.
- **STOP (set Idle)** — always available, ungated. You can also stop by cutting power or via the device's web UI.
- **Arming** (Bluetooth / BLE / WiFi / RC-Drone) is **inert scaffolding**: gated behind an RF-shielded-enclosure
  attestation and disabled until you **Load control map…** — a control map captured from your *own* device. The
  app supplies none and refuses to transmit without one (fail-safe).
- **Open control web UI** launches the device's own UI at `http://192.168.1.1` (its real control surface).
Full how-it-works/setup and the defensive **jammer-detection** guide are in the Cyber Controller hardware guides.

## Wardrive (lawful, owner-authorized)
GPS-tagged Wi-Fi survey exported as **WiGLE CSV** (upload at wigle.net). It passively logs broadcast
beacon metadata + your GPS position — it does **not** deauth or capture traffic.
1. Pick the **ESP32 (Marauder)** serial port and, if you have one, the **GPS (NMEA)** port.
2. Choose the output **WiGLE CSV** path.
3. **Start wardrive**. Rows are written only while there is a valid GPS fix (status shows the fix +
   AP count). **Stop** when done.

## Targets / Broadcast / Cross-Comm
- **Targets**: the shared, de-duplicated list of everything discovered; run actions against a target.
- **Broadcast**: fire one action across every connected radio at once.
- **Cross-Comm**: the event bus + auto-routing rules tying devices and tabs together.

## Health / Macros / Settings
- **Health**: resource + connection monitoring.
- **Macros**: record and replay command sequences.
- **Settings**: persisted preferences.

## Access gate & encrypted vault
If you set an admin password and/or a physical USB key, Cyber Controller is gated at launch: it stays
locked (and its vault data stays encrypted at rest) until the password and/or key is provided. There is
no "boot sequence" path around the gate — the app refuses to proceed unless the factor(s) are present.
Manage it from the command line: `--gate-status`, `--set-admin-password`, `--create-physical-key
--key-drive <dev>`, `--gate-policy {both|either|password|key}`, `--clear-gate`.

## Command-line quick reference
- `--list-os` / `--flash-os <id>` — list/flash OSes to USB (`--os-image`, `--os-sig`, `--target`, `--offline`, `--yes`).
- `--flash-tails` — flash Tails specifically (`--tails-image`, `--tails-sha256`, `--tails-sig`).
- `--deadman-setup` — Dead Man's Switch setup.
- Gate flags above.

## Staying current + offline
Profiles that track upstream releases and the OS resolver can check versions online. Pinned firmware
profiles stay on their configured build until the profile changes. A weekly repository job proposes
updated **OS catalog metadata** on a separate branch; it still needs to be merged and shipped in an
app update before installed copies receive it.

A version check does not download an offline image library. Firmware Vault update checks cover
profiles already cached, not the whole catalog. Prepare compatible local images and verification
files before going offline, and check that the interface you use supports that local-image path.
