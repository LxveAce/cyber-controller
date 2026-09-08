/* CI-runnable regression for the Cached tiles UI's pure logic. reform.js is a browser IIFE (it touches
 * document at load) so it can't be required in node; instead this extracts the ACTUAL pure functions from
 * its source bytes and exercises them — the honest status line (misses reported apart from errors; an
 * all-error view never claims an empty cache; an in-progress load isn't a false empty) and the S,W,N,E
 * parse. The full browser behaviour (no-request-on-open, decoded-only, cancel/stale, Stop/replacement,
 * layout) is proven separately in the private headless fixture. Filename ends .test.cjs so the baseline
 * CI glob tests/*.test.cjs runs it. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("fs"), path = require("path");

const SRC = fs.readFileSync(path.join(__dirname, "..", "src", "ui", "web", "static", "reform.js"), "utf8");

// Pull `function <name>(...) { ... }` out of the source by brace-matching, then realise it in this scope.
function extract(name) {
  const at = SRC.indexOf("function " + name + "(");
  assert.ok(at >= 0, "found function " + name + " in reform.js");
  let i = SRC.indexOf("{", at), depth = 0, end = -1;
  for (; i < SRC.length; i++) {
    if (SRC[i] === "{") depth++;
    else if (SRC[i] === "}") { depth--; if (depth === 0) { end = i + 1; break; } }
  }
  assert.ok(end > at, "matched braces for " + name);
  // eslint-disable-next-line no-new-func
  return new Function("return (" + SRC.slice(at, end) + ")")();
}

const statusText = extract("statusText");
const parseSWNE = extract("parseSWNE");
const planError = extract("planError");

// ── statusText: the honest status contract ──────────────────────────────────────────────────────────

test("statusText: an empty view has no tiles, an in-progress load shows progress not a false empty", () => {
  assert.equal(statusText({ cached: 0, miss: 0, error: 0, total: 0 }), "No tiles in this view.");
  assert.equal(statusText({ cached: 0, miss: 0, error: 0, total: 6 }), "Reading cache… 0/6 so far.");
  assert.equal(statusText({ cached: 2, miss: 0, error: 0, total: 6 }), "Reading cache… 2/6 so far.");
});

test("statusText: complete when every tile decoded", () => {
  assert.equal(statusText({ cached: 6, miss: 0, error: 0, total: 6 }), "Complete — all 6 tiles cached.");
});

test("statusText: partial reports genuine misses APART from errors", () => {
  assert.equal(statusText({ cached: 4, miss: 2, error: 0, total: 6 }), "Partial — 4 of 6 cached (2 not cached).");
  assert.equal(statusText({ cached: 4, miss: 0, error: 2, total: 6 }), "Partial — 4 of 6 cached (2 failed).");
  assert.equal(statusText({ cached: 3, miss: 2, error: 1, total: 6 }), "Partial — 3 of 6 cached (1 failed, 2 not cached).");
});

test("statusText: an all-error view says failed, NOT empty; a genuine miss-only view says empty", () => {
  assert.equal(statusText({ cached: 0, miss: 0, error: 6, total: 6 }),
    "Could not load tiles (6 failed) — press Show to retry.");
  assert.equal(statusText({ cached: 0, miss: 6, error: 0, total: 6 }), "No tiles cached for this area.");
});

// ── parseSWNE: token validation before coercion ──────────────────────────────────────────────────────

test("parseSWNE accepts four numeric tokens (trimmed) and rejects everything else", () => {
  assert.deepEqual(parseSWNE("40.70,-74.02,40.75,-73.96"), [40.70, -74.02, 40.75, -73.96]);
  assert.deepEqual(parseSWNE(" 35 , -12 , 60 , 30 "), [35, -12, 60, 30]);
  assert.equal(parseSWNE("abc"), null);
  assert.equal(parseSWNE("1,2,3"), null, "wrong count");
  assert.equal(parseSWNE("1,,3,4"), null, "empty coordinate not read as 0");
  assert.equal(parseSWNE("1,2,3,x"), null, "non-numeric");
  assert.equal(parseSWNE(""), null);
});

// ── planError: clear reasons for planView rejections ────────────────────────────────────────────────

test("planError maps planView reasons to clear messages", () => {
  assert.match(planError("invalid-bbox"), /S,W,N,E/);
  assert.match(planError("too-many-tiles"), /too large/i);
  assert.equal(planError("unknown-provider"), "Unknown provider.");
  assert.match(planError("something-else"), /something-else/);
});

// ── lifecycle regression: cancellation status + resize-refit guard ──────────────────────────────────
// These extract the ACTUAL decision functions from reform.js. The full DOM behaviour — a mode-switch
// mid-load settling to "Stopped." (late completions can't change it), a completed run keeping its honest
// status, and tiles re-fitting to the new-viewport same-zoom plan on resize (1000->700 => 442px, ~167.7px
// correction) — is proven in the private headless fixture; these guard the logic in CI.

const leaveStatus = extract("leaveStatus");
const refitGuard = extract("refitGuard");

test("leaveStatus: an in-progress run settles to 'Stopped.'; a settled run keeps its honest status", () => {
  assert.equal(leaveStatus({ cached: 0, miss: 0, error: 0, total: 2 }), "Stopped.", "nothing settled yet");
  assert.equal(leaveStatus({ cached: 1, miss: 0, error: 0, total: 2 }), "Stopped.", "partway through");
  assert.equal(leaveStatus({ cached: 2, miss: 0, error: 0, total: 2 }), null, "completed -> keep status");
  assert.equal(leaveStatus({ cached: 1, miss: 1, error: 0, total: 2 }), null, "settled partial -> keep status");
  assert.equal(leaveStatus({ cached: 0, miss: 0, error: 2, total: 2 }), null, "settled all-error -> keep status");
  assert.equal(leaveStatus(null), null, "no run -> nothing to settle");
});

test("refitGuard: refit only with a shown plan and a visible (non-zero) viewport", () => {
  assert.equal(refitGuard(null, 700, 400), false, "no shown plan yet");
  assert.equal(refitGuard({ bbox: [40, -74, 41, -73] }, 0, 400), false, "hidden pane (width 0)");
  assert.equal(refitGuard({ bbox: [40, -74, 41, -73] }, 700, 0), false, "collapsed (height 0)");
  assert.equal(refitGuard({ bbox: [40, -74, 41, -73] }, 442, 300), true, "shown + visible -> refit");
});
