# Bundled WPA wordlist core

These three small wordlists ship inside Cyber Controller so the offline WPA dictionary crack
(Crack Lab) works out of the box with no download. They are the tiny end of the `wordlist_manager`
catalog — larger lists (e.g. rockyou) are still downloaded on demand and integrity-checked.

| File | Lines | Purpose |
|---|---|---|
| `probable-v2-wpa-top62.txt` | 62 | Seconds-long smoke test |
| `probable-v2-wpa-top4800.txt` | 4,800 | Fast, high-yield WPA pass |
| `10k-most-common.txt` | 10,000 | Quick general first pass |

## Attribution

Sourced verbatim from **SecLists** (https://github.com/danielmiessler/SecLists), MIT License,
at pinned commit `acfed0cf1eecc1f8b412c8cd5085c3090494a1fa`. Each file's SHA-256 is pinned in
`src/core/wordlist_manager.py` (`CATALOG`); the bundled copies were fetched from that commit and
verified against those hashes.

SecLists is © its contributors and distributed under the MIT License. This vendored subset is
included under the same license.
