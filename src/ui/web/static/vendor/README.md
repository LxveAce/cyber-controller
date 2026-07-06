# Vendored third-party assets

These files are served from the app's own origin (`/static/vendor/…`) instead of a CDN, so the web
remote has **no external script dependency**: it works fully offline (a field security tool can't assume
internet), and there's no supply-chain surface from a compromised CDN — which is exactly what
`docs/WEBSITE-SECURITY.md` warns about.

## socket.io.min.js
- **Upstream:** https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.4/socket.io.min.js
- **Version:** Socket.IO v4.7.4 (client)
- **Size:** 49732 bytes
- **sha256:** `ad52fc540680945fe7549c0f1b1126b54029dd7eb25f8ce2b079a6242c807011`
- **sha384 (SRI):** `sha384-Gr6Lu2Ajx28mzwyVR8CFkULdCU7kMlZ9UthllibdOSo6qAiN+yXNHqtgdTvFXMT4`

Must stay in lockstep with the `python-socketio` server major version. On upgrade, re-download the exact
version, update this file + the hashes above, and re-verify the web terminal/flash-progress stream.
