# Running Cyber Controller on Windows — "is it safe?", SmartScreen, and antivirus

Short version: **Cyber Controller is open source, builds in front of you, and the download is exactly what
this repo compiles.** But the Windows `.exe` is **not code-signed yet**, so Windows SmartScreen and some
antivirus engines may warn about it or flag it as "unknown." That's expected for an unsigned,
freshly-built indie binary — here's *why* it happens and how to check for yourself.

---

## 1. "Windows protected your PC" (SmartScreen)

When you run the downloaded `.exe`, Windows may show a blue **"Windows protected your PC"** box and only
offer a **Don't run** button. It is **not** saying the file is malware — it's saying *"I don't recognize
this publisher yet."* SmartScreen builds trust from two things this app doesn't have yet: a **code-signing
certificate** and **download reputation** (how many people have run it). A brand-new solo project has
neither on day one.

**To run it anyway:**

1. In the "Windows protected your PC" box, click the small **More info** link.
2. A **Run anyway** button appears — click it.

That's it. You only have to do this once per version.

**If your browser blocks the download first** (Edge/Chrome sometimes say *"…isn't commonly downloaded"* or
*"Keep / Discard"*):

- **Edge:** open the **Downloads** flyout, find the file, click the **`···`** (or the warning), choose
  **Keep** → **Keep anyway**.
- **Chrome:** same idea — **`▾` / Keep** on the download bar, then **Keep**.

---

## 2. Why antivirus sometimes flags it (false positives)

Cyber Controller is a Python app packaged into a single `.exe` with **PyInstaller**. That bundling is the
honest, boring reason AV heuristics sometimes complain:

- **It's a self-extracting bundle.** A PyInstaller one-file exe carries a small bootloader that unpacks
  ~80 MB to a temp folder and runs it. "Small stub that unpacks and launches a payload" is *also* what a lot
  of malware does, so heuristic/ML engines flag the **pattern**, not anything actually malicious in this app.
- **It's unsigned.** No Authenticode certificate → no publisher identity → a reputation penalty.
- **It's low-prevalence.** AV engines trust files lots of people already run. A new release has been seen by
  almost nobody, so some engines flag it purely for being new.
- **It talks to serial ports + the network + writes to USB.** That's the whole point of a flasher, but it's
  also "suspicious behavior" to a generic heuristic.

**What this means in practice:** expect **a few engines out of ~70 to flag it, and the exact ones to differ
per file and per release.** A handful of heuristic hits (names like `Trojan.Generic`, `Wacatac`,
`ML.Attribute.HighConfidence`, `PUA`) on an unsigned PyInstaller build is the *normal* false-positive
signature — not evidence of a problem. Zero hits would actually be unusual for this kind of binary.

We don't hide this. The right response to "is it safe?" isn't "trust me" — it's "here's how to check."

### What the scans actually show

Every release was submitted to VirusTotal — the **per-file reports + SHA-256 are linked right in each
release's notes**. The pattern is exactly the false-positive signature above:

- **Windows `.exe`: ~1–5 of ~74 engines**, and always the same handful of heuristic/ML engines —
  **APEX, Bkav, Gridinsoft, Microsoft, Zillya**. Every mainstream engine (Kaspersky, ESET, BitDefender,
  Sophos, Symantec, Malwarebytes, …) returns clean.
- **Linux and macOS builds: 0 of ~74.** Clean across the board.

A few minor engines flagging an unsigned PyInstaller exe while every major one passes *is* the
false-positive pattern — not a real detection. Open the VirusTotal link on any release and read which
engines flagged it: generic/heuristic labels from a small minority is the expected noise.

### Latest release (v1.4.0) — actual results

| File | Detections | Flagged by |
|------|-----------|------------|
| `cyber-controller-v1.4.0-windows-x64.exe` | **4 / 74** | APEX, Bkav, Gridinsoft, Zillya |
| `cyber-controller-v1.4.0-linux-x64` | 0 / 74 | — clean |
| `cyber-controller-v1.4.0-linux-arm64` | 0 / 74 | — clean |
| `cyber-controller-v1.4.0-macos-arm64` | 0 / 74 | — clean |

(Per-file SHA-256 + the live VirusTotal report link are in each GitHub release's notes.)

### Why *those specific* engines flagged it (and why it's a false positive)

Across every release, the Windows hits only ever come from the same five heuristic/ML/reputation engines —
**APEX, Bkav, Gridinsoft, Microsoft, Zillya** — never from a signature match of a *named* malware family,
and never from a mainstream engine:

- **APEX** — a pure *machine-learning* engine (CyberArk). It scores a file by statistical resemblance to
  packed/obfuscated binaries. A PyInstaller bootloader wrapping a compressed Python runtime *looks* like a
  packer to the model, so it flags the **shape of the packaging, not the code**. Known for a high
  false-positive rate on legitimate packed apps.
- **Bkav (Pro)** — an aggressive heuristic/ML engine that emits generic `…AIDetectMalware` /
  `W64.AIDetect…` labels. It flags virtually *every* unsigned PyInstaller/Nuitka/packed Windows exe; it's one
  of the highest false-positive engines on VirusTotal.
- **Gridinsoft** — a heuristic engine that labels unsigned, packed installers/exes as a generic
  `PUA`/`Trojan.Heur` on sight — no specific family identified.
- **Microsoft Defender** — occasionally tags a *brand-new, unsigned, low-prevalence* build with a
  reputation/heuristic label (e.g. `Wacatac`/`Sabsik`). It's reputation-driven and clears as downloads
  accumulate or once the binary is code-signed. (It flagged a couple of older releases; **not** v1.4.0.)
- **Zillya** — a heuristic engine that flags packed/unsigned binaries generically.

Every one of those is a **heuristic / ML / reputation** detector, not a real signature hit. The three root
causes are all about *distribution*, not the program:

1. **PyInstaller "unpack-and-run" shape** — the one-file exe is a small stub that unpacks ~80 MB of
   compressed Python + DLLs to a temp dir and runs it. Malware packers do the same thing, so heuristics flag
   the pattern.
2. **No code-signing certificate** — unsigned ⇒ no publisher identity ⇒ a trust/reputation penalty.
3. **Low prevalence** — a fresh release has been seen by almost nobody, and some ML engines default-flag
   unknown packed exes.

The tell that it's a false positive: it's a **minority of engines, all heuristic, with generic labels**,
while **Kaspersky, ESET, BitDefender, Sophos, Symantec, Malwarebytes** and the rest of the majors return
clean — and the Linux/macOS builds (same source, same CI) score **0/74**. A real infection would be a named
family agreed on by many engines across platforms; this is the opposite. Code-signing (the roadmap item
below) is what makes even these heuristic engines stop.

---

## 3. Check it yourself (recommended)

**a) Verify the download is the real build.** Every release publishes a `SHA256SUMS.txt`. Compare:

```powershell
# PowerShell
Get-FileHash .\cyber-controller-vX.Y.Z-windows-x64.exe -Algorithm SHA256
```

The printed hash must match the line for that file in `SHA256SUMS.txt` on the release page. If it matches,
the file wasn't tampered with in transit — it's byte-for-byte what CI built from this source.

**b) Scan it on VirusTotal.** Either drag the `.exe` onto <https://www.virustotal.com/gui/home/upload>, or
paste its SHA-256 at <https://www.virustotal.com/gui/home/search>. The direct link for a known hash is:

```
https://www.virustotal.com/gui/file/<paste-the-SHA256-here>
```

Read the result the way a security person does: **a few heuristic/ML detections on an unsigned PyInstaller
exe is a false-positive pattern, not a verdict.** Look at *which* engines and *what* names — generic/ML
labels across a minority of engines is the expected noise; a specific, named, widely-agreed family would be
the thing to actually worry about (and would mean something is wrong with the build, which we'd want to know).

**c) Don't trust the binary at all — build it yourself.** It's open source:

```bash
git clone --recurse-submodules https://github.com/LxveAce/cyber-controller
cd cyber-controller
pip install .
python build.py        # produces the same dist/CyberController.exe
```

---

## 4. What we're doing to make the warnings go away

The warnings above are a *distribution* problem, not a code problem, and the fixes are on the roadmap:

- **A real signed installer.** A proper Windows installer (so the app shows up under *Settings → Apps →
  Installed apps* with an uninstaller) — see [`installer/`](../installer/). Signing it with an
  **OV/EV code-signing certificate** is what actually retires the SmartScreen prompt over time (EV gets
  reputation immediately; OV builds it as downloads accumulate).
- **Published SHA-256 checksums** on every release (so you can verify, per §3a).
- **A `--onedir` build** behind the installer for near-instant startup (no ~15 s self-extract) — which also
  makes the binary look less like a self-extracting stub to heuristics.

Until the signing cert is in place, the honest deal is: **the warnings are expected, here's exactly why, and
here are three independent ways to verify the file yourself.**

> Cyber Controller is an owner-only, defensive security tool. Use it only on hardware and networks you own
> or are explicitly authorized to test.
