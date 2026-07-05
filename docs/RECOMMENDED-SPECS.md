# Recommended specs & light-hardware guide

Cyber Controller runs on everything from a desktop workstation to a headless Raspberry Pi cyberdeck. It
ships four interchangeable frontends, so instead of a single hardware bar to clear you pick the frontend
that fits the machine you have. This page covers the **host** you run the controller on; for the boards and
firmware it flashes, see [HARDWARE-FIRMWARE-MATRIX.md](HARDWARE-FIRMWARE-MATRIX.md).

The guidance below is practical, not a benchmark table — treat the tiers as "which frontend for which
machine," not as guaranteed minimums.

## Pick a frontend

| Frontend | Launch | Extra deps | Best for |
|----------|--------|-----------|----------|
| **Full GUI** (PyQt5) | `--ui qt` (default) | none beyond the base install | Desktop/laptop. Every feature, the sidebar, persistent terminal, command palette. |
| **Lightweight GUI** (Tkinter) | `--ui tk` | none (Tkinter ships with Python) | Older machines, or anywhere PyQt5 is impractical to install. Core features. |
| **Terminal UI** (Textual) | `--ui tui` | `textual` | SSH sessions, headless servers, cyberdeck decks. Runs in any terminal. |
| **Web Remote** (Flask + SocketIO) | `--ui web` | `flask`, `flask-socketio` | A headless Pi you drive from a browser or phone on your network. |

Run with no `--ui` flag and a graphical picker lets you choose. Pass `--ui <name>` to go straight in — which
also means the terminal and web frontends never touch PyQt5, so they start on a machine that has no Qt at
all.

## Software floor

- **Python 3.12.**
- **Base dependencies** (installed for every frontend): PyQt5, pyserial, esptool, requests, psutil, and
  cryptography. `cryptography` is mandatory — secure storage uses authenticated AES-256-GCM and fails
  closed, there is no fallback.
- **Frontend extras** (install only what you'll use):

  ```
  pip install "cyber-controller[tui]"    # Terminal UI  (adds textual)
  pip install "cyber-controller[web]"    # Web Remote   (adds flask + flask-socketio)
  pip install "cyber-controller[full]"   # both of the above
  ```

  The Lightweight (Tkinter) frontend needs no extra — Tkinter comes with Python.

## Host tiers

### Desktop / laptop — the recommended experience
Windows, Linux, or macOS with the PyQt5 GUI. This is what the prebuilt binaries give you and it's the
smoothest way to flash and operate boards. Prebuilt downloads are published for **Windows x64**, **Linux
x64**, and **macOS arm64** on each release — no Python install required.

### Light / older hardware
If the full GUI is too heavy, or PyQt5 won't install cleanly, use `--ui tk`. The Tkinter frontend carries
the core flash/connect/operate features at a lower resource cost and leans only on the Python standard
library for its UI.

### Headless cyberdeck (Raspberry Pi and friends)
There's no prebuilt ARM binary — run from source. A Pi-class board handles the terminal or web frontends
comfortably:

- **Over SSH:** `cyber-controller --ui tui`
- **From a browser or phone on your LAN:**

  ```
  CC_WEB_ALLOW_LAN=1 cyber-controller --ui web --host 0.0.0.0 --port 5000
  ```

  The web server binds to `127.0.0.1` (local only) by default. Exposing it on the network is deliberately
  gated: you must set `CC_WEB_ALLOW_LAN=1` **and** pass `--host 0.0.0.0`. Put it behind TLS — you're
  driving hardware that can flash firmware and run wireless operations, so treat the control surface like
  the sensitive thing it is, and keep it to networks you trust.

## Connectivity, not just compute

Whatever the host, the real requirement is talking to boards: a working USB/serial stack and the right
drivers (CP210x, CH340, FTDI, and so on for your adapters). On Linux that usually means adding your user to
the `dialout` group so the serial ports are reachable without root. The multi-device wardrive and broadcast
features scale with how many boards you can physically attach — a powered USB hub matters more than CPU
once you're running several at once.
