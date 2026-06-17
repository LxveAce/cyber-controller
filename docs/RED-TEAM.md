# Red-Team Method & AI-Generated-Code Security Log

This project (and the Lxve web properties) are built with heavy AI assistance, so the security
review treats **AI-generated-code vulnerability patterns as a first-class threat model** — not an
afterthought. This document logs *how* the red-team is run and *what* the AI-codegen research found,
so the method is repeatable.

## Method

1. **Multi-angle adversarial review.** Each security pass fans out into independent reviewers, one
   per dimension (auth, supply-chain, crypto, injection, web, firmware-wipe, …). Each returns
   structured findings `{severity, where, description, fix, confidence}`. Findings are then
   **adversarially verified** (a second pass tries to *refute* each) before they are accepted, so
   plausible-but-wrong findings don't drive churn.
2. **The AI-generated-code lens.** Because LLM-generated code has *characteristic* failure modes
   (below), every web/API surface is checked against the AI-codegen checklist explicitly — these are
   the bugs "the happy path looks fine" hides.
3. **Inline red-team verification.** Security-critical behaviors are exercised directly (tamper
   detection, auth bypass attempts, injection payloads, SSRF/path-traversal probes) — see the
   `tests/` suite and the documented inline checks.
4. **Tooling.** Run a scanner built for AI-generated code against the deployed surface:
   - **VibeScan** (free) — SSRF / XSS / SQLi / missing headers / exposed secrets / CSRF.
   - **VAS — Vibe App Scanner** (vibeappscanner.com) — scans the *deployed* site, no source needed.
   - **AquilaX Vibe Code Security** (aquilax.ai/vibe), **VibeSecurity** (IDE real-time).
   - **Semgrep / ZeroPath** for authorization / IDOR logic that scanners miss.

## AI-generated-code vulnerability checklist (the threat model)

> Research backing: >40% of AI-generated code contains security flaws; across 5,600 vibe-coded apps,
> 2,000+ vulnerabilities, 400+ exposed secrets, 175 exposed-PII instances. LLMs default to maximum
> permissiveness because it "works" and doesn't error during development.

| # | AI-codegen pattern | Risk | How cyber-controller addresses it |
|---|--------------------|------|-----------------------------------|
| 1 | **Missing input sanitization** (most common) | injection | port validated against the device registry; command length-capped; control chars rejected (`SerialConnection.write`); `profile_id` checked against known profiles |
| 2 | **Missing authentication/authorization** | full takeover | SocketIO `connect`/events authenticated + port-scoped; HTTP routes `@requires_auth` |
| 3 | **Hardcoded secrets / default creds** | account takeover | removed `admin/cyber`; one-time generated password; scrypt hash; constant-time compare; secret key persisted 0600 |
| 4 | **IDOR / broken object-level auth** | data access | single-operator model; every action port-scoped to a registered device (no cross-object refs). Documented assumption — a multi-user deployment would need per-principal checks |
| 5 | **`Access-Control-Allow-Origin: *`** | CSWSH / data theft | explicit CORS allowlist; never `*` |
| 6 | **Missing CSRF** | forced actions | per-session CSRF token on POSTs + socket handshake |
| 7 | **Missing security headers** | XSS/clickjacking | CSP, X-Frame-Options DENY, nosniff, Referrer-Policy, Permissions-Policy |
| 8 | **SSRF in "fetch a URL" features** | internal pivot | GitHub host allowlist + redirect validation + size cap in the firmware vault / flash core |
| 9 | **XSS from unescaped output** | session theft | over-the-air scan data rendered via DOM `textContent`, never `innerHTML` concat |
| 10 | **Verbose error leakage** | info disclosure | generic 500s; exception detail logged server-side only |
| 11 | **Unverified supply chain** | RCE-on-device | SHA-256 pinning + name-matched assets (no `assets[0]` fallback); no unauthenticated XOR crypto |
| 12 | **Prompt injection** (AI-integrated features) | instruction hijack | N/A — cyber-controller has no LLM-in-the-loop feature; flagged for any future "AI assistant" addition |

**Verification result:** classes 1–11 are addressed in the codebase; class 4 is documented as a
single-operator assumption; class 12 is N/A today. Re-run the checklist on every new web/API surface.

## See also

- `SECURITY.md` — the shipped hardening summary + disclosure policy.
- `docs/WEBSITE-SECURITY.md` — applying this research to lxveace.com + esp32marauder.com.

## Sources

- [Endor Labs — Most common vulnerabilities in AI-generated code](https://www.endorlabs.com/learn/the-most-common-security-vulnerabilities-in-ai-generated-code)
- [arXiv 2504.20612 — Hidden risks of LLM-generated web application code](https://arxiv.org/html/2504.20612v1)
- [Invicti — Vibe coding security checklist](https://www.invicti.com/blog/web-security/vibe-coding-security-checklist-how-to-secure-ai-generated-apps)
- [Checkmarx — Security in vibe coding](https://checkmarx.com/blog/security-in-vibe-coding/)
- [ZeroPath — Authorization bugs / IDOR crisis 2025](https://zeropath.com/blog/idor-crisis-2025)
- [Semgrep — Can LLMs detect IDORs?](https://semgrep.dev/blog/2025/can-llms-detect-idors-understanding-the-boundaries-of-ai-reasoning/)
- [OWASP GenAI Exploit Round-up Q1 2026](https://genai.owasp.org/2026/04/14/owasp-genai-exploit-round-up-report-q1-2026/)
- Scanners: [VibeScan](https://vibesecurity.net/) · [VAS / Vibe App Scanner](https://vibeappscanner.com/best-ai-security-scanner) · [AquilaX Vibe](https://aquilax.ai/vibe)
