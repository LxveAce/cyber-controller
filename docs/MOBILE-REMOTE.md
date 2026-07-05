# Mobile remote — drive Cyber Controller from your phone

Cyber Controller already ships a hardened, mobile-first **web UI** (Flask + Socket.IO). A phone browser on
the same LAN can drive the whole controller today — flashing, targets, terminal, nodes, a one-tap **Remote**,
and a navigable **Device View** — with **zero native code**. This guide covers exposing it safely and
installing it as a home-screen app (PWA).

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
export CC_WEB_USER=admin
export CC_WEB_PASS='choose-a-strong-one'
export CC_WEB_ALLOW_LAN=1
export CC_WEB_CERT=$PWD/certs/cc.pem
export CC_WEB_KEY=$PWD/certs/cc-key.pem
cyber-controller --ui web --host 0.0.0.0 --port 8443     # from a source checkout: python -m src.app --ui web …
```

(`--host`/`--port` default to `127.0.0.1:5000`; the flags above expose it on the LAN on 8443.)

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

## 5. The qFlipper-on-phone experience — Remote + Device View

Two touch-first surfaces turn the phone into a proper handheld controller. Both are reached from the web UI's
**Remote** nav link, both sit behind the same auth, and both fire through the same guarded send path:

- **Remote home (`/remote`)** — a one-tap quick-command grid. For every **connected** device it lists that
  firmware's argument-free commands, grouped by category, sourced straight from the real protocol registry, so
  a button can never fire a command the firmware doesn't have. Tap to send.
- **Device View (`/device/<port>`)** — a navigable reconstruction of the firmware's on-screen menu (an honest
  *skin*, not a pixel mirror). Drill through the menu; a leaf fires that firmware's real serial command.
  Reached from each device's **Device View ›** link on the Remote page.

Both fire through the existing guarded **`POST /api/command`** (auth + CSRF + rate-limit + control-char
validation). Commands the safety classifier flags **`lab-only`** / **`illegal-tx`** are **labelled and
confirmed before sending** — never blocked (the "proceed" path is always offered). Argument-taking commands are
shown but not fired from a tap (they need a value — use the terminal for those).

> The reconstructed menu and the quick-command catalog back the **Qt, web, and Tkinter** frontends from one
> UI-agnostic core (`src/core/device_menus.py`, `src/core/quick_commands.py`) — the phone gets the exact same
> command set as the desktop, with no duplicated definitions to drift out of sync.

## 6. What's deliberately deferred

- **Bundling `socket.io` locally** (drop the CDN for a stricter CSP + full offline). Deferred: it means
  vendoring a pinned minified blob and keeping it in lockstep with the flask-socketio server version — a
  maintenance gate, tracked separately.

## 7. Native app?

The PWA remote here is the supported way to drive Cyber Controller from a phone today, and it covers a lot:
the Remote grid and Device View reuse 100% of the hardened web UI, cross-platform, with no native code.

A dedicated native companion app is a **separate project in its own repo** — aimed at direct phone↔device
transports a browser can't reach (Bluetooth now, cellular later). It is **not part of this release and isn't
usable yet**; when it's ready it ships on its own, not bundled here. Until then, the PWA remote above is the
way to use CC from a phone.
