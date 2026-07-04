# PWA app icons — owner-drop

The installable mobile remote (PWA) references two icons that are intentionally **not** committed, because
they are the **LxveAce brand mark** (owner-supplied art — this tooling must not fabricate the brand identity):

| file            | size    | notes                                                        |
| --------------- | ------- | ------------------------------------------------------------ |
| `ace-192.png`   | 192×192 | maskable-safe: keep the ace inside the centered ~80% safe zone |
| `ace-512.png`   | 512×512 | maskable-safe                                                |

Drop the real **purple ace-of-spades** PNGs here at those exact names and sizes. Until then:

- the `manifest.webmanifest` is still valid; **iOS Safari** *Add to Home Screen* works and just shows a
  default icon;
- **Android Chrome will NOT offer "Install app"** until at least one PNG icon (≥144px) is fetchable — its
  installability check requires a real icon, so `beforeinstallprompt` won't fire while these 404. The site
  still works as a normal responsive page; dropping the PNGs enables the Android install prompt;
- the service worker precache is **best-effort per asset** (`Promise.allSettled` + `cache.add`), so a
  missing icon does **not** break the worker or the offline shell.

No SVG/logo is generated here on purpose — see the project's asset rule: use the real file, don't recreate it.
