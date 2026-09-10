/* Isolated tests for offline_metadata.js using node:test with an inert fake DOM + mock fetch.
 * No real browser/DOM/network; no app/router. Verifies: no request on open; Analyze sends only the pasted
 * bytes to the same-origin endpoint with the CSRF header; distinct states; text-only render incl. structured
 * (nested) values; the UTF-8 byte cap enforced BEFORE any request; edit-state settling; leave/return + Clear +
 * navigation stale-response invalidation; and exact large-integer fidelity from the ACTUAL response text. */
"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const path = require("path");

const mod = require(path.join(__dirname, "..", "src", "ui", "web", "static", "offline_metadata.js"));

function El(id) {
  this.id = id; this._attrs = {}; this._listeners = {}; this.textContent = ""; this.value = "";
  this._classes = new Set();
  this.classList = {
    contains: (c) => this._classes.has(c),
    add: (c) => this._classes.add(c),
    remove: (c) => this._classes.delete(c),
  };
}
El.prototype.setAttribute = function (k, v) { this._attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; };
El.prototype.addEventListener = function (t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); };
El.prototype.fire = function (t) { (this._listeners[t] || []).forEach((fn) => fn({})); };

function makeEnv() {
  const reg = {};
  ["od-input", "od-analyze", "od-clear", "od-status", "od-detail"].forEach((i) => (reg["#" + i] = new El(i)));
  const root = new El("view-offline-data");
  root._classes.add("on"); // active view
  root.querySelector = (sel) => reg[sel] || null;
  const doc = { querySelector: () => null };
  const calls = [];
  const pending = [];
  const fetchImpl = (url, opts) => { calls.push({ url, opts }); return new Promise((resolve, reject) => pending.push({ resolve, reject })); };
  return { root, reg, doc, calls, pending, fetchImpl };
}
function respond(env, i, ok, status, bodyObj) {
  env.pending[i].resolve({ ok, status, text: () => Promise.resolve(JSON.stringify(bodyObj)) });
}
function respondText(env, i, ok, status, bodyText) {
  env.pending[i].resolve({ ok, status, text: () => Promise.resolve(bodyText) });
}
// outcome-clarity helpers: reject the fetch promise, or resolve a received response whose body read rejects.
function rejectFetch(env, i, err) { env.pending[i].reject(err); }
function respondTextReject(env, i, ok, status, err) {
  env.pending[i].resolve({ ok, status, text: () => Promise.reject(err) });
}
const tick = () => new Promise((r) => setTimeout(r, 0));
function withFetch(fetchImpl, fn) {
  const pf = global.fetch, pw = global.window;
  global.fetch = fetchImpl; global.window = { CSRF_TOKEN: "test-csrf" };
  try { return fn(); } finally { global.fetch = pf; global.window = pw; }
}
// Force the TextEncoder-absent fallback path.
function withoutTextEncoder(fn) {
  const saved = global.TextEncoder;
  global.TextEncoder = undefined;
  try { return fn(); } finally { global.TextEncoder = saved; }
}

const META = JSON.stringify({
  global: { "core:datatype": "cf32_le", "core:version": "1.2.6", "vendor:details": { name: "nested-sentinel", items: [1, 2] } },
  captures: [{ "core:sample_start": 0 }], annotations: [],
});
function summarizedResult() {
  return {
    status: "summarized",
    summary: {
      format: "SigMF", version: "1.2.6", conformance_note: "checked descriptors only",
      datatype: { raw: "cf32_le", complex: true, element_format: "f32", bytes_per_channel_sample: 8, byte_order: "little" },
      num_channels: null, recognized_global: {},
      uninterpreted_global: { "vendor:details": { value: { name: "nested-sentinel", items: [1, 2] }, interpreted: false, semantics: "unknown" } },
      captures: { count: 1, truncated: false, entries: [{ recognized: { "core:sample_start": 0 }, uninterpreted: {} }] },
      annotations: { count: 0, truncated: false, entries: [] },
      extensions_opaque: [], inert_references: {},
    },
    diagnostics: [], diagnostics_truncated: false,
  };
}

test("no request on create (view open)", () => {
  const env = makeEnv();
  withFetch(env.fetchImpl, () => {
    const h = mod.create(env.doc, env.root);
    assert.ok(h && typeof h.syncVisibility === "function");
    assert.strictEqual(env.calls.length, 0);
    assert.strictEqual(env.root.getAttribute("data-state"), "empty");
  });
});

test("analyze with empty input does not fetch", () => {
  const env = makeEnv();
  withFetch(env.fetchImpl, () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = "";
    h.analyze();
    assert.strictEqual(env.calls.length, 0);
  });
});

test("analyze sends exactly the pasted bytes with CSRF header + octet-stream", () => {
  const env = makeEnv();
  withFetch(env.fetchImpl, () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = META;
    h.analyze();
    assert.strictEqual(env.calls.length, 1);
    const c = env.calls[0];
    assert.strictEqual(c.url, "/api/offline-metadata/summarize");
    assert.strictEqual(c.opts.headers["X-CSRF-Token"], "test-csrf");
    assert.strictEqual(c.opts.headers["Content-Type"], "application/octet-stream");
    assert.strictEqual(c.opts.body, META);
    assert.strictEqual(env.root.getAttribute("data-state"), "busy");
  });
});

test("summarized renders text-only; nested structured value visible (not [object Object])", async () => {
  const env = makeEnv();
  await withFetch(env.fetchImpl, async () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = META; h.analyze();
    respond(env, 0, true, 200, summarizedResult());
    await tick();
    assert.strictEqual(env.root.getAttribute("data-state"), "summarized");
    const d = env.reg["#od-detail"].textContent;
    assert.ok(d.indexOf("[object Object]") === -1, "structured value collapsed to [object Object]");
    assert.ok(d.indexOf("nested-sentinel") !== -1, "nested value content missing");
    assert.ok(d.indexOf("items") !== -1 && d.indexOf("[1,2]") !== -1, "nested structure/values missing");
  });
});

test("unsupported and invalid states are distinct", async () => {
  for (const [status, state] of [["unsupported", "unsupported"], ["invalid", "invalid"]]) {
    const env = makeEnv();
    await withFetch(env.fetchImpl, async () => {
      const h = mod.create(env.doc, env.root);
      env.reg["#od-input"].value = META; h.analyze();
      respond(env, 0, true, 200, { status, diagnostics: [{ field: "f", code: "c", detail: "d" }], diagnostics_truncated: false, summary: null });
      await tick();
      assert.strictEqual(env.root.getAttribute("data-state"), state);
    });
  }
});

test("transport error (413) -> request-failure", async () => {
  const env = makeEnv();
  await withFetch(env.fetchImpl, async () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = META; h.analyze();
    respond(env, 0, false, 413, { error: "payload-too-large" });
    await tick();
    assert.strictEqual(env.root.getAttribute("data-state"), "request-failure");
    assert.match(env.reg["#od-status"].textContent, /payload-too-large/);
  });
});

test("Clear discards a late result", async () => {
  const env = makeEnv();
  await withFetch(env.fetchImpl, async () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = META; h.analyze();
    h.clear();
    respond(env, 0, true, 200, summarizedResult());
    await tick();
    assert.strictEqual(env.root.getAttribute("data-state"), "empty");
    assert.strictEqual(env.reg["#od-detail"].textContent, "");
  });
});

test("leave (syncVisibility) then return does NOT reanimate the old request", async () => {
  const env = makeEnv();
  await withFetch(env.fetchImpl, async () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = META; h.analyze(); // pending
    env.root._classes.delete("on"); h.syncVisibility(); // navigate away -> abort + invalidate + settle
    assert.notStrictEqual(env.root.getAttribute("data-state"), "busy", "left view still busy");
    env.root._classes.add("on");                        // return to the view
    respond(env, 0, true, 200, summarizedResult());     // the old request finally resolves
    await tick();
    assert.notStrictEqual(env.root.getAttribute("data-state"), "summarized", "stale request repainted after return");
    assert.strictEqual(env.reg["#od-detail"].textContent, "");
  });
});

test("editing while pending settles out of busy (no permanent busy)", async () => {
  const env = makeEnv();
  await withFetch(env.fetchImpl, async () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = META; h.analyze();     // busy
    env.reg["#od-input"].value = META + " "; env.reg["#od-input"].fire("input");
    assert.notStrictEqual(env.root.getAttribute("data-state"), "busy", "stuck busy after edit");
    respond(env, 0, true, 200, summarizedResult());     // the pre-edit response
    await tick();
    assert.notStrictEqual(env.root.getAttribute("data-state"), "summarized", "pre-edit response painted");
  });
});

test("editing after a completed result marks it stale", async () => {
  const env = makeEnv();
  await withFetch(env.fetchImpl, async () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = META; h.analyze();
    respond(env, 0, true, 200, summarizedResult());
    await tick();
    assert.strictEqual(env.root.getAttribute("data-state"), "summarized");
    env.reg["#od-input"].value = META + "x"; env.reg["#od-input"].fire("input");
    assert.strictEqual(env.root.getAttribute("data-state"), "empty", "stale result not cleared on edit");
    assert.strictEqual(env.reg["#od-detail"].textContent, "");
  });
});

test("over-cap input is rejected BEFORE any fetch", () => {
  const env = makeEnv();
  withFetch(env.fetchImpl, () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = "a".repeat(262145); // 262145 ASCII bytes > 262144 cap
    h.analyze();
    assert.strictEqual(env.calls.length, 0, "over-cap input still fetched");
    assert.strictEqual(env.root.getAttribute("data-state"), "request-failure");
    assert.match(env.reg["#od-status"].textContent, /exceeds the 262144-byte limit/);
  });
});

test("exact large integer preserved from the ACTUAL response text (not a rounded JS object)", async () => {
  const env = makeEnv();
  await withFetch(env.fetchImpl, async () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = META; h.analyze();
    // The backend emits an out-of-JS-range integer as an exact decimal STRING (lossless display contract).
    const bodyText = '{"status":"summarized","summary":{"format":"SigMF","version":"1.2.6","conformance_note":"n",'
      + '"datatype":{"raw":"cf32_le","complex":true,"element_format":"f32","bytes_per_channel_sample":8,"byte_order":"little"},'
      + '"num_channels":null,"recognized_global":{},"uninterpreted_global":{},'
      + '"captures":{"count":1,"truncated":false,"entries":[{"recognized":{"core:sample_start":"9007199254740993"},"uninterpreted":{}}]},'
      + '"annotations":{"count":0,"truncated":false,"entries":[]},"extensions_opaque":[],"inert_references":{}},'
      + '"diagnostics":[],"diagnostics_truncated":false}';
    respondText(env, 0, true, 200, bodyText);
    await tick();
    assert.strictEqual(env.root.getAttribute("data-state"), "summarized");
    assert.ok(env.reg["#od-detail"].textContent.indexOf("9007199254740993") !== -1, "exact large integer not displayed");
  });
});

// ---- UTF-8 byte-cap counting: the TextEncoder-absent fallback must count BYTES, not UTF-16 code units ----

test("fallback (no TextEncoder): over-cap multibyte rejected before any fetch", () => {
  withoutTextEncoder(() => {
    const env = makeEnv();
    withFetch(env.fetchImpl, () => {
      const h = mod.create(env.doc, env.root);
      env.reg["#od-input"].value = "界".repeat(87382); // 87382 * 3 = 262146 UTF-8 bytes > 262144 cap
      h.analyze();
      assert.strictEqual(env.calls.length, 0, "over-cap multibyte fetched (fallback counted code units)");
      assert.strictEqual(env.root.getAttribute("data-state"), "request-failure");
      assert.match(env.reg["#od-status"].textContent, /262146 bytes; exceeds the 262144-byte limit/);
    });
  });
});

test("fallback (no TextEncoder): exactly-at-cap input IS sent (boundary positive control)", () => {
  withoutTextEncoder(() => {
    const env = makeEnv();
    withFetch(env.fetchImpl, () => {
      const h = mod.create(env.doc, env.root);
      env.reg["#od-input"].value = "界".repeat(87381) + "a"; // 87381*3 + 1 = 262144 == cap
      h.analyze();
      assert.strictEqual(env.calls.length, 1, "at-cap input wrongly rejected (fallback over-counts)");
      assert.strictEqual(env.root.getAttribute("data-state"), "busy");
    });
  });
});

test("fallback (no TextEncoder): surrogate-pair char counts as 4 bytes", () => {
  withoutTextEncoder(() => {
    const env = makeEnv();
    withFetch(env.fetchImpl, () => {
      const h = mod.create(env.doc, env.root);
      // U+1D7D8 = surrogate pair (2 code units) -> 4 UTF-8 bytes. Code-unit counting would send this.
      env.reg["#od-input"].value = "𝟘".repeat(65537); // 65537 * 4 = 262148 bytes > cap
      h.analyze();
      assert.strictEqual(env.calls.length, 0, "surrogate pair miscounted");
      assert.match(env.reg["#od-status"].textContent, /262148 bytes; exceeds the 262144-byte limit/);
    });
  });
});

test("fallback (no TextEncoder): unpaired surrogate counts as U+FFFD (3 bytes)", () => {
  withoutTextEncoder(() => {
    const env = makeEnv();
    withFetch(env.fetchImpl, () => {
      const h = mod.create(env.doc, env.root);
      // Lone high surrogate -> TextEncoder emits U+FFFD (3 bytes); the fallback must match.
      env.reg["#od-input"].value = "\uD800".repeat(87382); // 87382 * 3 = 262146 bytes > cap
      h.analyze();
      assert.strictEqual(env.calls.length, 0, "unpaired surrogate miscounted");
      assert.match(env.reg["#od-status"].textContent, /262146 bytes; exceeds the 262144-byte limit/);
    });
  });
});

test("primary path (TextEncoder present): over-cap multibyte rejected before fetch (byte-based, not code-unit)", () => {
  const env = makeEnv();
  withFetch(env.fetchImpl, () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = "界".repeat(87382); // 262146 bytes; code-unit count 87382 would send
    h.analyze();
    assert.strictEqual(env.calls.length, 0, "primary path counted code units, not bytes");
    assert.match(env.reg["#od-status"].textContent, /262146 bytes; exceeds the 262144-byte limit/);
  });
});

// ---- outcome clarity: honest failure/uncertain copy (repair CC-OFFLINE-OUTCOME) ----
// Only the documented status/error pairs are proven pre-summary rejections; unknown errors, mismatched
// pairs, rejected fetches and unreadable responses are reported as "unconfirmed", never as a proven
// rejection or a proven network failure.

test("outcome clarity: known fixed rejection (415/unsupported-content-type) keeps definite rejected-before-analysis", async () => {
  const env = makeEnv();
  await withFetch(env.fetchImpl, async () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = META; h.analyze();
    respond(env, 0, false, 415, { error: "unsupported-content-type" });
    await tick();
    assert.strictEqual(env.root.getAttribute("data-state"), "request-failure");
    assert.match(env.reg["#od-status"].textContent, /unsupported-content-type/);
    assert.match(env.reg["#od-detail"].textContent, /rejected before analysis/);
    assert.match(env.reg["#od-detail"].textContent, /No metadata was interpreted/);
  });
});

test("outcome clarity: known fixed rejection (413/payload-too-large) keeps definite copy", async () => {
  const env = makeEnv();
  await withFetch(env.fetchImpl, async () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = META; h.analyze();
    respond(env, 0, false, 413, { error: "payload-too-large" });
    await tick();
    assert.strictEqual(env.root.getAttribute("data-state"), "request-failure");
    assert.match(env.reg["#od-detail"].textContent, /rejected before analysis/);
  });
});

test("outcome clarity: unknown HTTP error (500, non-JSON body) -> unconfirmed, not a proven rejection", async () => {
  const env = makeEnv();
  await withFetch(env.fetchImpl, async () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = META; h.analyze();
    respondText(env, 0, false, 500, "upstream boom");
    await tick();
    assert.strictEqual(env.root.getAttribute("data-state"), "request-failure");
    assert.match(env.reg["#od-status"].textContent, /unconfirmed/);
    assert.match(env.reg["#od-status"].textContent, /http-500/);
    assert.match(env.reg["#od-detail"].textContent, /could not be confirmed/);
    assert.strictEqual(env.reg["#od-detail"].textContent.indexOf("rejected before analysis"), -1, "overclaimed rejection for unknown error");
  });
});

test("outcome clarity: known error code on unexpected status (500 + invalid-body) -> unconfirmed", async () => {
  const env = makeEnv();
  await withFetch(env.fetchImpl, async () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = META; h.analyze();
    respond(env, 0, false, 500, { error: "invalid-body" });
    await tick();
    assert.strictEqual(env.root.getAttribute("data-state"), "request-failure");
    assert.match(env.reg["#od-status"].textContent, /unconfirmed/);
    assert.strictEqual(env.reg["#od-detail"].textContent.indexOf("rejected before analysis"), -1, "known error on wrong status overclaimed rejection");
  });
});

test("outcome clarity: documented status with undocumented error (400 + surprise-error) -> unconfirmed", async () => {
  const env = makeEnv();
  await withFetch(env.fetchImpl, async () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = META; h.analyze();
    respond(env, 0, false, 400, { error: "surprise-error" });
    await tick();
    assert.strictEqual(env.root.getAttribute("data-state"), "request-failure");
    assert.match(env.reg["#od-status"].textContent, /unconfirmed/);
    assert.match(env.reg["#od-status"].textContent, /surprise-error/);
    assert.strictEqual(env.reg["#od-detail"].textContent.indexOf("rejected before analysis"), -1);
  });
});

test("outcome clarity: rejected fetch -> unconfirmed, not a proven network failure", async () => {
  const env = makeEnv();
  await withFetch(env.fetchImpl, async () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = META; h.analyze();
    rejectFetch(env, 0, new TypeError("Failed to fetch"));
    await tick();
    assert.strictEqual(env.root.getAttribute("data-state"), "request-failure");
    assert.match(env.reg["#od-status"].textContent, /unconfirmed/);
    assert.match(env.reg["#od-detail"].textContent, /could not be confirmed/);
    assert.strictEqual(env.reg["#od-detail"].textContent.indexOf("No metadata was interpreted"), -1, "catch overclaimed no-metadata-interpreted");
  });
});

test("outcome clarity: response-text rejection -> unconfirmed (received-but-unreadable; not labeled unreachable)", async () => {
  const env = makeEnv();
  await withFetch(env.fetchImpl, async () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = META; h.analyze();
    respondTextReject(env, 0, true, 200, new Error("stream read error"));
    await tick();
    assert.strictEqual(env.root.getAttribute("data-state"), "request-failure");
    assert.match(env.reg["#od-status"].textContent, /unconfirmed/);
    assert.strictEqual(env.reg["#od-detail"].textContent.indexOf("Could not reach the backend"), -1, "labeled a received-but-unreadable response as unreachable");
  });
});

test("outcome clarity: busy -> request-failure with exactly one submission and no automatic retry", async () => {
  const env = makeEnv();
  await withFetch(env.fetchImpl, async () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = META; h.analyze();
    assert.strictEqual(env.root.getAttribute("data-state"), "busy");
    assert.strictEqual(env.calls.length, 1);
    respondText(env, 0, false, 500, "boom");
    await tick();
    assert.strictEqual(env.root.getAttribute("data-state"), "request-failure");
    assert.strictEqual(env.calls.length, 1, "an automatic retry was issued after a failure");
  });
});

test("outcome clarity: a late fetch rejection after Clear is not painted", async () => {
  const env = makeEnv();
  await withFetch(env.fetchImpl, async () => {
    const h = mod.create(env.doc, env.root);
    env.reg["#od-input"].value = META; h.analyze();
    h.clear();
    rejectFetch(env, 0, new TypeError("late network error"));
    await tick();
    assert.strictEqual(env.root.getAttribute("data-state"), "empty", "late rejection repainted a discarded request");
    assert.strictEqual(env.reg["#od-detail"].textContent, "");
  });
});
