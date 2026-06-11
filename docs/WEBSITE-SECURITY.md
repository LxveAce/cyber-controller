# Website Security Plan — lxveace.com & esp32marauder.com

Both sites are AI-assisted and statically hosted (GitHub Pages style). Static marketing sites have a
**much smaller attack surface** than the web app the rest of this repo hardens — there is no
server-side auth, database, or session, so the heavy AI-codegen classes (missing auth, IDOR, SQLi,
SSRF) largely don't apply. The relevant subset for static AI-made sites is below, with a prioritized
plan. Research + method: see `docs/RED-TEAM.md`.

## Relevant AI-codegen risks for STATIC sites

| Risk | Applies because | 
|------|-----------------|
| **Missing security headers / weak CSP** | LLMs rarely emit CSP/HSTS/Permissions-Policy for static templates |
| **Client-side XSS via injected data** | both `downloads`/`builds` pages fetch **live GitHub release JSON** and render it — if injected via `innerHTML`, a crafted release/asset name executes script |
| **Third-party JS without SRI** | any CDN `<script>` (analytics, socket.io, fonts) is a supply-chain foothold if unpinned |
| **Exposed secrets in client JS** | LLMs embed API keys/PATs in front-end code; GitHub fetches must use the **unauthenticated public API only** |
| **Supply chain / stale deps** | unpinned CDN versions, no Dependabot |
| **GitHub Pages exposure** | `.git`, source maps, backup files, or an unenforced-HTTPS custom domain |

## Current posture (from recon)

- **lxveace.com** — already strong: strict `Content-Security-Policy: default-src 'none'`,
  `Permissions-Policy` locking camera/mic/geolocation, `Referrer-Policy: strict-origin-when-cross-origin`,
  OG/Twitter cards. Contact is `mailto:` (no server form). **Verify** the downloads page renders the
  live GitHub release data with `textContent`/DOM, not `innerHTML`.
- **esp32marauder.com** — static hub with `downloads.html` / `builds.html` that fetch live GitHub
  releases ("Fetching latest release…"). **Headers/CSP not yet confirmed** — this is the priority to verify.

## Plan (priority order)

1. **Security headers on BOTH sites.** GitHub Pages cannot set HTTP headers directly, so:
   - If fronted by **Cloudflare**: add a Transform Rule / Response Header Modification setting
     `Content-Security-Policy` (allow `self` + only the exact CDNs used), `Strict-Transport-Security: max-age=63072000; includeSubDomains; preload`,
     `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
     `Permissions-Policy: geolocation=(), microphone=(), camera=()`, `Cross-Origin-Opener-Policy: same-origin`.
   - If not on Cloudflare: replicate the `lxveace.com` `<meta http-equiv>` CSP on esp32marauder.com,
     and consider moving DNS to Cloudflare for real header control + HSTS preload.
2. **Kill client-side XSS in the release renderers.** On every page that injects GitHub API data
   (`downloads.html`, `builds.html`), build rows with `document.createElement` + `textContent`
   (never `el.innerHTML = '...' + release.name`). A malicious asset/release name is then inert.
   (This is the exact fix applied to the controller's `targets.html`.)
3. **SRI + pin every third-party script.** Add `integrity=` + `crossorigin="anonymous"` to all CDN
   `<script>`/`<link>`; pin exact versions; drop anything unused. Self-host where practical.
4. **No secrets in client JS.** Confirm the GitHub fetches use the public unauthenticated API (no PAT
   in front-end), and grep both repos for embedded tokens/keys before each deploy.
5. **Supply chain.** Enable Dependabot + GitHub security alerts on `LxveAce.github.io` and the
   `esp32marauder.com` repo; pin any build deps.
6. **GitHub Pages hardening.** Enforce HTTPS (repo Settings → Pages → "Enforce HTTPS"); ensure no
   `.git`, `.env`, source maps, or backup files are published; verify `robots.txt`/`sitemap.xml` only
   expose intended paths.
7. **Scan the deployed sites** with a vibe-coding scanner that needs no source access:
   **[VibeScan](https://vibesecurity.net/)** (free) and **[VAS](https://vibeappscanner.com/best-ai-security-scanner)**
   against both live URLs; triage findings back into this plan.
8. **Forms.** Keep contact as `mailto:` (no server endpoint = no injection). If a real form is ever
   added, route it through a vetted provider with validation + rate-limit + bot protection.

## Quick wins (do first)
- Confirm/add the CSP + HSTS + nosniff + frame-deny headers on **esp32marauder.com** (lxveace.com
  already has most).
- Audit the **release-rendering JS** on both downloads/builds pages for `innerHTML` → switch to
  `textContent`.
- Add **SRI** to any CDN script and **enforce HTTPS** on both Pages sites.

> These are *planning* recommendations — say the word and I'll implement the ones in the website
> repos (`LxveAce.github.io`, `esp32marauder.com`) directly.
