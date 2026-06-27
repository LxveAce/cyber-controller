# Cyber Controller — How-To

Cyber Controller is an all-in-one controller, flasher, logger, and pentest GUI for ESP32 security
gear and cyberdecks. Everything works **offline**; online features (latest firmware/OS versions) are
conveniences, never requirements. **Lawful, owner-authorized use only.**

Hover any button or field to see a tooltip explaining what it does. The tabs across the top are:

## Flash (firmware)
Write firmware to a connected board (ESP32 Marauder, GhostESP, Bruce, etc.).
1. Plug in the board; pick its **Port** (Refresh re-scans).
2. Pick a **Firmware Profile** and, if your board has a screen (CYD/M5/…), the matching **Board /
   variant** (Auto guesses per-chip and can be wrong for display boards).
3. **Flash**. Use **Firmware Vault → Download to Vault** to cache firmware for offline flashing later.
4. Optional **Dead Man's Switch**: enable it to weave the anti-forensic wipe into the flash (a setup
   dialog opens first).

## Software OS (flash an OS to USB)
Write a verified bootable operating system to a **USB stick** (separate from board firmware).
1. Pick an OS — **Tails** (amnesiac/Tor), **Kali** (pentest), **Arch** (general).
2. **Check latest** resolves the current version from the official source (or tick **Use bundled
   version (offline)** to use the version shipped with the app).
3. Pick the **target USB** (only removable drives are listed; the whole drive is erased) — or **Use
   local image…** if you already downloaded one.
4. **Flash OS**. The image is integrity-verified (SHA-256 + OpenPGP signature) before any write.

## Devices
Connect to and control attached radios/boards: open a serial console, send commands, and watch live
output. Targets discovered here are shared across the app via the Target Pool.

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
- `--list-os` / `--flash-os <id>` — list/flash OSes to USB (`--os-image`, `--os-target`, `--offline`, `--yes`).
- `--flash-tails` — flash Tails specifically (`--tails-image`, `--tails-sha256`, `--tails-sig`).
- `--deadman-setup` — Dead Man's Switch setup.
- Gate flags above.

## Staying current + offline
The firmware/OS catalog refreshes automatically (a weekly job updates the bundled versions; the app
also checks live). With no internet, everything falls back to the bundled catalog and any cached
images — you can still flash in the field.
