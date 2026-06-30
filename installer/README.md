# Windows installer (Inno Setup)

This builds a real Windows installer so Cyber Controller shows up under **Settings → Apps → Installed apps**
with a proper icon and an uninstaller — instead of a loose `.exe` the user has to manage by hand.

## What it does
- Packages a **`--onedir`** PyInstaller build (instant startup; no ~15 s self-extract).
- Installs per-user to `%LOCALAPPDATA%\Programs\CyberController` (**no admin/UAC prompt**).
- Creates Start-menu (and optional desktop) shortcuts.
- Registers the standard **Add/Remove Programs** keys automatically (Inno writes `DisplayName`,
  `DisplayVersion`, `Publisher`, `DisplayIcon`, `UninstallString`, `InstallLocation`, `EstimatedSize`), so
  the app appears in *Installed apps* and uninstalls cleanly.

## Build it locally
```bat
:: 1) produce the folder build the installer packages
python build.py --onedir

:: 2) compile the installer (needs Inno Setup 6 — https://jrsoftware.org/isdl.php)
iscc /DMyAppVersion=1.4.0 installer\cyber-controller.iss
```
Output: `installer\Output\cyber-controller-v1.4.0-windows-x64-setup.exe`.

CI does the same on every release (see `.github/workflows/build-release.yml`).

## Not done here (cert-gated)
The installer is **not code-signed**. An unsigned installer still triggers SmartScreen until an **OV/EV
code-signing certificate** is applied (`signtool sign /fd sha256 ...` on `Output\*.exe`). That's the real
fix for the "Windows protected your PC" prompt — see [`../docs/WINDOWS-SECURITY.md`](../docs/WINDOWS-SECURITY.md).
