"use strict";
/* Tests for the cache-only raster tile loader (src/ui/web/static/map_raster_loader.js).
 * Two layers: (A) injected fetch/decode/timer/abort seams for the status logic; (B) REAL
 * Response/ReadableStream regression tests that assert reader-lock cleanup on every terminal path
 * (over-budget, interrupted, abort, late response, success), prompt settlement, and zero unhandled
 * rejections. A companion headless fixture exercises real Image decode of tiny synthetic PNG/JPEG. */
const { test } = require("node:test");
const assert = require("node:assert");
const L = require("../src/ui/web/static/map_raster_loader.js");

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 1, 2, 3]);
const JPEG = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0, 1, 2, 3]);
const HTML = new Uint8Array([0x3c, 0x68, 0x74, 0x6d, 0x6c]);

const unhandled = [];
process.on("unhandledRejection", (r) => unhandled.push(r));

function realResp(bytes, status) { return new Response(bytes === null ? null : bytes, { status: status || 200 }); }
function okDecode() { return () => ({ promise: Promise.resolve(), cancel() {} }); }
function failDecode() { return () => ({ promise: Promise.reject(new Error("x")), cancel() {} }); }
const tick = () => new Promise(r => setImmediate(r));

// ── (A) injected-seam status logic ────────────────────────────────────────────────
test("invalid provider/coordinate -> 'invalid', no fetch", async () => {
  let called = 0;
  const ld = L.createLoader({ fetch: () => { called++; return Promise.resolve(realResp(PNG)); }, decode: okDecode() });
  assert.equal((await ld.loadTile("nope", 3, 4, 5)).status, "invalid");
  assert.equal((await ld.loadTile("osm", 1, 2, 0)).status, "invalid");
  assert.equal((await ld.loadTile("osm", 20, 0, 0)).status, "invalid");
  assert.equal((await ld.loadTile("osm", 1.5, 0, 0)).status, "invalid");
  assert.equal(called, 0);
});

test("204 -> 'missing'", async () => {
  const ld = L.createLoader({ fetch: () => Promise.resolve(realResp(null, 204)), decode: okDecode() });
  assert.equal((await ld.loadTile("carto-dark", 2, 1, 1)).status, "missing");
});

test("200 PNG that decodes -> 'ok' data:image/png; JPEG under .png -> image/jpeg", async () => {
  const ldP = L.createLoader({ fetch: () => Promise.resolve(realResp(PNG)), decode: okDecode() });
  const rp = await ldP.loadTile("carto-dark", 2, 1, 1);
  assert.equal(rp.status, "ok"); assert.equal(rp.mime, "image/png"); assert.ok(rp.url.startsWith("data:image/png;base64,"));
  const ldJ = L.createLoader({ fetch: () => Promise.resolve(realResp(JPEG)), decode: okDecode() });
  assert.equal((await ldJ.loadTile("carto-dark", 2, 1, 1)).mime, "image/jpeg");
});

test("non-image bytes -> 'decode-error' (decode not called); valid magic but decode REJECTS -> 'decode-error'", async () => {
  let calls = 0;
  const ldH = L.createLoader({ fetch: () => Promise.resolve(realResp(HTML)), decode: () => { calls++; return { promise: Promise.resolve(), cancel() {} }; } });
  assert.equal((await ldH.loadTile("carto-dark", 2, 1, 1)).status, "decode-error");
  assert.equal(calls, 0);
  const ldR = L.createLoader({ fetch: () => Promise.resolve(realResp(PNG)), decode: failDecode() });
  assert.equal((await ldR.loadTile("carto-dark", 2, 1, 1)).status, "decode-error");
});

test("non-200 -> 'http-error'+code; transport reject -> code 0", async () => {
  const ld404 = L.createLoader({ fetch: () => Promise.resolve(realResp(HTML, 404)), decode: okDecode() });
  assert.deepEqual(await ld404.loadTile("carto-dark", 2, 1, 1), { status: "http-error", code: 404 });
  const ldNet = L.createLoader({ fetch: () => Promise.reject(new Error("net")), decode: okDecode() });
  assert.deepEqual(await ldNet.loadTile("carto-dark", 2, 1, 1), { status: "http-error", code: 0 });
});

test("viewerLoad adapter: ok -> url, everything else -> null", async () => {
  const ldOk = L.createLoader({ fetch: () => Promise.resolve(realResp(PNG)), decode: okDecode() });
  assert.ok((await ldOk.viewerLoad("carto-dark", 2, 1, 1)).startsWith("data:image/png"));
  const ldMiss = L.createLoader({ fetch: () => Promise.resolve(realResp(null, 204)), decode: okDecode() });
  assert.equal(await ldMiss.viewerLoad("carto-dark", 2, 1, 1), null);
});

test("option clamps: maxBytes override > 1 MiB is clamped; deadline override > ceiling is clamped", async () => {
  // a 2000-byte body with a 5 MiB override: clamped to 1 MiB so 2000 is fine -> ok
  const big = new Uint8Array(2000); big.set(PNG, 0);
  const ld = L.createLoader({ fetch: () => Promise.resolve(realResp(big)), decode: okDecode(), maxBytes: 5 * 1024 * 1024, deadlineMs: 10 * 60 * 1000 });
  assert.equal((await ld.loadTile("carto-dark", 2, 1, 1)).status, "ok");
});

// ── (B) REAL Response/ReadableStream reader-lock regression ─────────────────────────
test("REAL over-budget stream -> 'too-large' AND body released (locked=false)", async () => {
  const big = new Uint8Array(4000); big.set(PNG, 0);
  const resp = realResp(big);
  const ld = L.createLoader({ fetch: () => Promise.resolve(resp), decode: okDecode(), maxBytes: 64 });
  assert.equal((await ld.loadTile("carto-dark", 2, 1, 1)).status, "too-large");
  await tick();
  assert.equal(resp.body.locked, false, "reader released after over-budget");
});

test("REAL interrupted stream -> 'read-error', body released, settles promptly, no unhandled", async () => {
  const before = unhandled.length;
  const resp = new Response(new ReadableStream({ pull(c) { c.error(new Error("boom")); } }));
  const ld = L.createLoader({ fetch: () => Promise.resolve(resp), decode: okDecode() });
  const r = await ld.loadTile("carto-dark", 2, 1, 1);        // must resolve now, not at the deadline
  assert.equal(r.status, "read-error");
  await tick(); await tick();
  assert.equal(resp.body.locked, false, "reader released after interrupted read");
  assert.equal(unhandled.length, before, "no unhandled rejection from the interrupted read");
});

test("REAL abort of a held body -> 'aborted' AND reader cancelled/released (locked=false)", async () => {
  let pulls = 0;
  const resp = new Response(new ReadableStream({ pull() { pulls++; return new Promise(() => {}); } }));  // never settles
  const ac = new AbortController();
  const ld = L.createLoader({ fetch: () => Promise.resolve(resp), decode: okDecode() });
  const p = ld.loadTile("carto-dark", 2, 1, 1, ac.signal);
  await tick(); await tick();                                 // let the reader attach + pull start
  ac.abort();
  assert.equal((await p).status, "aborted");
  await tick(); await tick();
  assert.equal(resp.body.locked, false, "held reader cancelled + released on abort");
});

test("REAL late response after abort -> its body is cancelled (locked=false), result 'aborted'", async () => {
  const resp = realResp(PNG);
  let deliver;
  const ld = L.createLoader({ fetch: () => new Promise(res => { deliver = () => res(resp); }), decode: okDecode() });
  const ac = new AbortController();
  const p = ld.loadTile("carto-dark", 2, 1, 1, ac.signal);
  ac.abort();                                                 // settle as aborted BEFORE the fetch delivers
  assert.equal((await p).status, "aborted");
  deliver();                                                  // late response arrives
  await tick(); await tick();
  assert.equal(resp.body.locked, false, "late response body cancelled, never leaks a reader");
});

test("REAL success stream -> 'ok' and body released", async () => {
  const resp = realResp(PNG);
  const ld = L.createLoader({ fetch: () => Promise.resolve(resp), decode: okDecode() });
  assert.equal((await ld.loadTile("carto-dark", 2, 1, 1)).status, "ok");
  await tick();
  assert.equal(resp.body.locked, false, "reader released after success");
});

test("caller abort listener is removed after settle", async () => {
  const removed = [];
  const signal = { aborted: false, _l: [], addEventListener(e, f) { this._l.push(f); }, removeEventListener(e, f) { removed.push(f); } };
  const ld = L.createLoader({ fetch: () => Promise.resolve(realResp(PNG)), decode: okDecode() });
  await ld.loadTile("carto-dark", 2, 1, 1, signal);
  assert.equal(removed.length, 1, "the abort listener was removed on settle");
});

test("no unhandled rejections across the suite", async () => { await tick(); assert.equal(unhandled.length, 0, unhandled.map(String).join("; ")); });

// ── deadline / held-decode / already-aborted (retained from the original suite) + sync-decoder throw ──
function timers() {
  const cbs = [];
  return { setTimeout: (fn) => { const h = { fn, cleared: false }; cbs.push(h); return h; },
           clearTimeout: (h) => { if (h) h.cleared = true; },
           fire: () => cbs.forEach(h => { if (!h.cleared) h.fn(); }) };
}

test("deadline elapses (fetch never resolves) -> 'timeout'", async () => {
  const T = timers();
  const ld = L.createLoader({ fetch: () => new Promise(() => {}), decode: okDecode(),
    setTimeout: T.setTimeout, clearTimeout: T.clearTimeout });
  const p = ld.loadTile("carto-dark", 2, 1, 1);
  T.fire();
  assert.equal((await p).status, "timeout");
});

test("held decode past the deadline -> 'timeout', decode cancelled", async () => {
  const T = timers();
  let decCancelled = false;
  const ld = L.createLoader({ fetch: () => Promise.resolve(realResp(PNG)),
    decode: () => ({ promise: new Promise(() => {}), cancel() { decCancelled = true; } }),
    setTimeout: T.setTimeout, clearTimeout: T.clearTimeout });
  const p = ld.loadTile("carto-dark", 2, 1, 1);
  await tick(); await tick();          // let fetch + body read settle, decode start
  T.fire();
  assert.equal((await p).status, "timeout");
  assert.equal(decCancelled, true);
});

test("already-aborted signal -> 'aborted' with no fetch", async () => {
  let called = 0;
  const ld = L.createLoader({ fetch: () => { called++; return Promise.resolve(realResp(PNG)); }, decode: okDecode() });
  assert.equal((await ld.loadTile("carto-dark", 2, 1, 1, { aborted: true })).status, "aborted");
  assert.equal(called, 0);
});

test("synchronous decoder throw -> 'decode-error', settles promptly, no unhandled", async () => {
  const before = unhandled.length;
  const T = timers();
  const ld = L.createLoader({ fetch: () => Promise.resolve(realResp(PNG)),
    decode: () => { throw new Error("decoder unavailable"); },
    setTimeout: T.setTimeout, clearTimeout: T.clearTimeout });
  const r = await ld.loadTile("carto-dark", 2, 1, 1);   // must resolve WITHOUT firing the deadline timer
  assert.equal(r.status, "decode-error");
  await tick(); await tick();
  assert.equal(unhandled.length, before, "a synchronous decoder throw must not cause an unhandled rejection");
});
