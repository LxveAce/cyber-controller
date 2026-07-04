# Mobile remote — drive Cyber Controller from your phone

Cyber Controller already ships a hardened, mobile-first **web UI** (Flask + Socket.IO). A phone browser on
the same LAN can drive the whole controller today — flashing, targets, terminal, nodes — with **zero native
code**. This guide covers exposing it safely and installing it as a home-screen app (PWA).

> **Safety first.** The web UI drives real attack hardware. It binds to `127.0.0.1` by default and refuses a
> non-local bind unless you opt in explicitly. Only expose it on a network you trust, and always with TLS.

---

## 1. Prerequisites — auth + TLS come FIRST

Set these **before** you expose anything. (A service worker / installable PWA will not even register without
a secure context — see §4 — so do TLS before you try to "add to home screen".)

| env var                  | purpose                                                                 |
| ------------------------ | ----------------------------------------------------------------------- |
| `CC_WEB_USER`            | admin username (required for a real deployment)                         |
| `CC_WEB_PASS`            | admin password. **If unset, a random one is printed at startup** — set it. |
| `CC_WEB_ALLOW_LAN=1`     | explicit opt-in to bind off-localhost. Without it a non-local bind is refused. |
| `CC_WEB_CERT` / `CC_WEB_KEY` | paths to a TLS cert + key. Strongly recommended; **required** for the PWA. |
| `CC_WEB_ORIGINS`         | optional extra CORS origins (comma-separated). The allowlist is never `*`. |

Auth is HTTP Basic on the first request, then a **session cookie** — so you authenticate once, not per asset.

## 2. Launch it on the LAN

```bash
# from the repo root
export CC_WEB_USER=admin
export CC_WEB_PASS='choose-a-strong-one'
export CC_WEB_ALLOW_LAN=1
export CC_WEB_CERT=$PWD/certs/cc.pem
export CC_WEB_KEY=$PWD/certs/cc-key.pem
python -m cyber_controller --ui web --host 0.0.0.0 --port 8443
```

Then browse to `https://<desktop-LAN-IP>:8443` from your phone and log in.

> Binding `--host 0.0.0.0` without `CC_WEB_ALLOW_LAN=1` is refused by design. Running the Flask **dev**
> server off-localhost additionally requires `CC_WEB_ALLOW_DEV_SERVER=1` (use a real WSGI/TLS front for
> anything beyond a lab).

## 3. TLS cert for a LAN IP (mkcert)

Browsers only grant a **secure context** (needed for the service worker) over HTTPS or on `localhost`. For a
LAN IP, mint a locally-trusted cert with [mkcert](https://github.com/FiloSottile/mkcert):

```bash
mkcert -install                      # once, adds a local CA to your trust store
mkcert -cert-file certs/cc.pem -key-file certs/cc-key.pem 192.168.1.50 localhost
```

Install the mkcert root CA on the phone too (mkcert prints its location) so the phone trusts the cert. A
plain self-signed cert also works but the phone will warn until you trust it.

## 4. Install as a home-screen app (PWA)

Once you're on `https://…` and logged in:

- **iOS Safari:** Share → *Add to Home Screen*.
- **Android Chrome:** ⋮ → *Install app* / *Add to Home screen*.

The app installs standalone (no browser chrome), themed LxveAce purple (`#a371f7`). What makes this work:

- **`/manifest.webmanifest`** — app metadata (name, `display: standalone`, theme, icons). Public, no secrets.
- **`/sw.js`** — the service worker, served from the origin root so it controls the whole app. It caches
  **only the static app shell** (CSS, manifest, icons). It **never** caches authenticated `/api/` responses
  or the live `/socket.io/` serial stream — that data stays off disk by construction.
- **App icons** are owner-supplied — drop the real ace-of-spades PNGs into `src/ui/web/static/icons/`
  (see the README there). **iOS** *Add to Home Screen* works without them (default icon); **Android Chrome
  won't offer "Install app" until a real ≥144px PNG icon is fetchable** — so drop the PNGs to enable the
  Android install prompt.

> **Why TLS is not optional for the PWA:** service workers require a secure context. On a bare `http://`
> LAN IP the registration silently declines (the code swallows it), the site still works as a normal
> responsive web page, but it won't install offline-capable. `localhost` is the only non-TLS exemption.

## 5. What's deliberately deferred

- **Bundling `socket.io` locally** (drop the CDN for a stricter CSP + full offline). Deferred: it means
  vendoring a pinned minified blob and keeping it in lockstep with the flask-socketio server version — a
  maintenance gate, tracked separately.
- **A touch-first "Remote" home + web Device View** (the qFlipper-on-phone experience) — shares work with the
  DV / CP clusters; a later phase.

## 6. Native app?

**PWA-first is the recommendation:** it reuses 100% of the hardened web UI, needs no native code, and is
cross-platform. A native app (React Native / Flutter) is only worth it if a **direct phone↔device BLE GATT**
transport is later prioritized — a separate transport, not a reskin of this remote.
