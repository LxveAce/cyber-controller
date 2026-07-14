# Website Security Plan — lxveace.com, esp32marauder.com & microprojects.net

All three sites are AI-assisted and statically hosted (GitHub Pages). Static marketing sites have a
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

## Current posture (VERIFIED 2026-07-13 — full source audit, beat 236; microprojects.net added 2026-07-14, beat 251)

The source repos (`LxveAce.github.io`, `esp32marauder.com`, `microprojects.net`) were audited file-by-file. **Every in-repo
item on the plan below is now implemented and verified** — the only remaining gaps require real HTTP
response headers (a Cloudflare/DNS action) or a live-URL scan, both owner-gated.

- **lxveace.com (`LxveAce.github.io`)** — strong. Strict `default-src 'none'` CSP + `Permissions-Policy`
  on every page (`index`, `downloads/`, `showcase`, `privacy`, `terms`, `disclaimer`, `404`). The
  downloads page (`downloads/index.html`) carries the exact `connect-src https://api.github.com` its
  release fetch needs, and its renderer (`downloads.js`) escapes **every** GitHub-release field:
  `escHtml()` (textNode round-trip) on `tag_name`/`asset.name`, `escAttr(safeUrl(...))` on URLs,
  numeric-only for sizes/dates/counts, and `repo` is a hardcoded constant — so a malicious release/asset
  name is inert. `mailto:`-only contact (no server form). `.well-known/security.txt` present.
- **esp32marauder.com** — strong (upgraded since the Jul-7 recon, which had CSP "not yet confirmed").
  Strict `default-src 'none'` CSP + `Permissions-Policy` on **all 8 pages**; `downloads.html` carries
  `connect-src https://api.github.com`. Its renderer (`downloads.js`) escapes every release field via
  `esc()` + `safeUrl()`; `script.js` uses `createElement`/`textContent` + trusted `data-*` only, and
  `builds.html` is static (no live fetch). `.well-known/security.txt` + `robots.txt` present; secret
  scan clean (the showcase submission form posts to a Cloudflare Worker — secrets stay server-side).
- **microprojects.net** — strong (source audited 2026-07-14, mirrors the other two). Strict
  `default-src 'none'` CSP with a tight `form-action https://submit.lxvelabs.com` + `connect-src 'none'`
  on all 6 pages (`index`, `showcase`, `privacy`, `terms`, `disclaimer`, `404`). No external/CDN
  `<script>`/`<link>` — fonts and JS are self-hosted, so there is nothing to SRI-pin. No inline secrets
  (the showcase form posts to the shared LxveLabs submission Worker; secrets stay server-side). The
  lightbox renderer (`script.js`) builds each media element with `createElement` + trusted `data-*`
  attributes + `textContent`, and its only `innerHTML` write is a `= ''` clear on close — so there is
  no DOM-XSS sink and no remote-data path (`connect-src 'none'`, no `fetch`). `.well-known/security.txt`
  + `robots.txt` + `sitemap.xml` present; no `.map`/`.env`/`.bak`/source-map artifacts published.

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

## Verified status (2026-07-13, beat 236 — source audit of both repos)

| Plan item | Status |
|-----------|--------|
| 1. Security headers | ✅ **In-repo done** — strict meta CSP + `Permissions-Policy` on every page of both sites. ⚠ **Owner-gated remainder:** `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`, and `X-Frame-Options`/`frame-ancestors` **cannot** be set via `<meta http-equiv>` (browsers ignore them there) — they need real HTTP response headers, i.e. Cloudflare in front of GitHub Pages (a DNS/owner decision). |
| 2. Kill client-side XSS in release renderers | ✅ **Done + verified** — both `downloads.js` files escape every GitHub-release field (`escHtml`/`esc` + `escAttr` + `safeUrl`); numeric/hardcoded fields are inert. No unescaped remote string reaches `innerHTML`. |
| 3. SRI + pin third-party scripts | ✅ **N/A** — neither site loads any external/CDN `<script>`/`<link>` (CSP is `script-src 'self'`; audit found zero third-party origins). Nothing to pin. |
| 4. No secrets in client JS | ✅ **Verified** — GitHub fetches use the unauthenticated public API (no PAT); secret scan clean; the one form posts to a Cloudflare Worker (secrets server-side). |
| 5. Supply chain | ✅ **Low surface** — self-hosted JS/CSS/fonts, no build deps in either Pages repo to pin. |
| 6. GitHub Pages hardening | ✅ **Done** — `.nojekyll` serves `.well-known/`; `.well-known/security.txt` + `robots.txt` present; no `.git`/`.env`/`.map`/`.bak` published. ⚠ "Enforce HTTPS" toggle lives in repo **Settings → Pages** (owner-gated, not a file change). |
| 7. Scan the deployed sites | ⚠ **Owner-gated** — VibeScan/VAS run against live URLs (deployment), not source. |
| 8. Forms | ✅ **Done** — `mailto:` + vetted Cloudflare Worker; no in-page server endpoint. |

**Net:** the website source repos are hardened — no in-repo fix is outstanding. What remains is exclusively
owner-gated infra: put all three domains behind **Cloudflare** for real response headers (HSTS + nosniff +
frame-deny), flip **Settings → Pages → Enforce HTTPS**, and run the **live-URL scanners**. A 2026-07-14
live check of `esp32marauder.com` CONFIRMED the deployed response is bare GitHub Pages (`Server: GitHub.com`,
no Cloudflare) with no `Strict-Transport-Security`, no `X-Content-Type-Options: nosniff`, and no
`X-Frame-Options` — so item #1 is still open at the HTTP layer for all three GitHub-Pages domains.

> These were *planning* recommendations; the in-repo ones are now applied + verified (above). Re-audit
> after any redesign of the downloads/builds release-rendering JS (the only remote-data sink).
