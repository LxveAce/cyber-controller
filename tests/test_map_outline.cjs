/* Tests for the offline maps overview helper and its browser controller. Pure geometry runs against the
 * ACTUAL bundled Natural Earth outline plus small synthetic polygons; the controller runs against a
 * minimal DOM/timer/fetch stub with controlled time — no real DOM, no real network, no real waits. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("fs"), path = require("path");

// create()'s draw() calls document.createElementNS; stub it before requiring anything that draws.
globalThis.document = { createElementNS: () => ({ setAttribute() {} }) };

const outline = require("../src/ui/web/static/map_outline.js");

const GEOJSON = path.join(__dirname, "..", "src", "config", "maps", "world_110m.geojson");
const world = JSON.parse(fs.readFileSync(GEOJSON, "utf8"));

const flush = () => new Promise((res) => setImmediate(res));
const nums = (d) => (d.match(/-?\d+(?:\.\d+)?/g) || []).map(Number);
const enc = (s) => new TextEncoder().encode(s);

// The exact existing Flock overlay projection (reform.js render(cams,bbox,you) toXY), re-derived here so
// the test proves map_outline.project reproduces it — no silent Web Mercator switch.
function flockToXY(lat, lon, bbox) {
  const s = bbox[0], w = bbox[1], n = bbox[2], e = bbox[3];
  const lonSpan = (e - w) || 1e-6, latSpan = (n - s) || 1e-6;
  return [((lon - w) / lonSpan) * 1000, (1 - (lat - s) / latSpan) * 380];
}

// ── pure projection / parsing ─────────────────────────────────────────────────────────────────────

test("project reproduces the Flock overlay toXY exactly at 1000x380", () => {
  const boxes = [[-90, -180, 90, 180], [40.70, -74.02, 40.75, -73.96], [35, -12, 60, 30]];
  const pts = [[0, 0], [51.5, -0.12], [-33.9, 151.2], [40.72, -74.0], [180, 90]];
  for (const b of boxes) for (const [lat, lon] of pts)
    assert.deepEqual(outline.project(lat, lon, b, 1000, 380), flockToXY(lat, lon, b));
});

test("project honours an explicit width/height", () => {
  const b = [-90, -180, 90, 180];
  assert.deepEqual(outline.project(0, 0, b, 800, 600), [400, 300]);
  assert.deepEqual(outline.project(90, 180, b, 800, 600), [800, 0]);
});

test("validateBbox accepts an ordered area and rejects wrapping/inverted/out-of-range/NaN", () => {
  assert.deepEqual(outline.validateBbox([35, -12, 60, 30]), [35, -12, 60, 30]);
  assert.equal(outline.validateBbox([60, -12, 35, 30]), null, "inverted lat");
  assert.equal(outline.validateBbox([35, 30, 60, -12]), null, "wrapping lon");
  assert.equal(outline.validateBbox([-95, -12, 60, 30]), null, "S below -90");
  assert.equal(outline.validateBbox([35, -12, NaN, 30]), null, "NaN");
  assert.equal(outline.validateBbox([35, -12, 60]), null, "wrong length");
});

test("parseBbox validates the four tokens before coercion — an empty field is not read as 0", () => {
  assert.deepEqual(outline.parseBbox("35,-12,60,30"), [35, -12, 60, 30]);
  assert.deepEqual(outline.parseBbox(" 35 , -12 , 60 , 30 "), [35, -12, 60, 30], "trims whitespace");
  assert.equal(outline.parseBbox("35,,60,30"), null, "empty coordinate rejected (not 0)");
  assert.equal(outline.parseBbox("35,-12,60"), null, "wrong count");
  assert.equal(outline.parseBbox("35,-12,60,x"), null, "non-numeric");
  assert.equal(outline.parseBbox("35,-12,60,3.0.1"), null, "malformed number");
  assert.equal(outline.parseBbox("60,-12,35,30"), null, "delegates ordering to validateBbox");
});

// ── clipping (the core correction) ──────────────────────────────────────────────────────────────────

test("clipSegment returns the colinear visible portion, or null when fully outside", () => {
  assert.deepEqual(outline.clipSegment(100, 100, 900, 300, 0, 0, 1000, 380), [[100, 100], [900, 300]],
    "a fully-inside segment is unchanged");
  assert.equal(outline.clipSegment(-50, -50, -10, -10, 0, 0, 1000, 380), null, "fully outside -> null");
  const seg = outline.clipSegment(-100, 190, 1100, 190, 0, 0, 1000, 380);
  assert.deepEqual(seg, [[0, 190], [1000, 190]], "crossing segment clipped to the viewport edges");
});

test("a clipped diagonal keeps its true viewport crossing, not a clamped one", () => {
  // Ordinary closed triangle through bbox [-1,-1,1,1] in 1000x380. The diagonal must cross x=500 at
  // y=190, NOT y=-47.5 (a vertex-clamping artefact), and no vertex may land far outside like -4000/2090.
  const fc = { type: "FeatureCollection", features: [
    { geometry: { type: "Polygon", coordinates: [[[-10, -10], [10, 10], [10, -10], [-10, -10]]] } },
  ] };
  const paths = outline.featuresToPaths(fc, [-1, -1, 1, 1], 1000, 380);
  assert.ok(paths.length >= 1, "the triangle produces a visible clipped path");
  const all = paths.flatMap(nums);
  assert.ok(Math.min(...all) >= -0.01 && Math.max(...all) <= 1000.01, "coords stay inside the viewport");
  assert.ok(all.every((v) => v >= -0.01 && v <= 1000.01), "no huge clamped coordinates like 4000/2090");

  // Find the sub-path segment that spans x=500 and interpolate its y — it must be ~190.
  let found = null;
  for (const d of paths) {
    const cmds = d.match(/[ML]-?\d+(?:\.\d+)? -?\d+(?:\.\d+)?/g) || [];
    const verts = cmds.map((c) => nums(c));
    for (let i = 0; i + 1 < verts.length; i++) {
      const [x0, y0] = verts[i], [x1, y1] = verts[i + 1];
      if ((x0 - 500) * (x1 - 500) <= 0 && x0 !== x1) {
        found = y0 + ((500 - x0) / (x1 - x0)) * (y1 - y0);
      }
    }
  }
  assert.ok(found !== null, "some clipped segment spans x=500");
  assert.ok(Math.abs(found - 190) < 0.5, `diagonal crosses x=500 at y≈190, got ${found}`);
});

test("featuresToPaths over the ACTUAL bundled outline: finite paths clipped inside the viewport", () => {
  const paths = outline.featuresToPaths(world, outline.WORLD, 1000, 380);
  assert.ok(paths.length > 100, `expected many rings, got ${paths.length}`);
  for (const d of paths) {
    assert.ok(/^M/.test(d), "each path starts with M");
    assert.ok(!/[^ML\d.\- ]/.test(d), "only M/L commands and numbers");
    for (const v of nums(d)) assert.ok(Number.isFinite(v), "finite output");
  }
  const all = paths.flatMap(nums);
  assert.ok(Math.min(...all) >= -0.01 && Math.max(...all) <= 1000.01, "world coords within the viewport");
});

test("an ordinary country-scale view renders finite bounded paths", () => {
  const paths = outline.featuresToPaths(world, [35, -12, 60, 30], 1000, 380);   // Europe-ish
  assert.ok(paths.length > 0, "some coastline is visible");
  for (const v of paths.flatMap(nums)) assert.ok(v >= -0.01 && v <= 1000.01 && Number.isFinite(v));
});

test("antimeridian features stay finite and bounded (numeric finiteness only, not seam topology)", () => {
  const mp = world.features.filter((f) => f.geometry.type === "MultiPolygon");
  assert.ok(mp.length > 0);
  const paths = outline.featuresToPaths({ type: "FeatureCollection", features: mp }, outline.WORLD, 1000, 380);
  for (const v of paths.flatMap(nums)) assert.ok(Number.isFinite(v) && v >= -0.01 && v <= 1000.01);
});

test("a polygon hole yields two clipped ring paths; a fully-outside ring is culled", () => {
  const fc = { type: "FeatureCollection", features: [
    { geometry: { type: "Polygon", coordinates: [
      [[-10, -10], [10, -10], [10, 10], [-10, 10], [-10, -10]],
      [[-2, -2], [2, -2], [2, 2], [-2, 2], [-2, -2]],
    ] } },
    { geometry: { type: "Polygon", coordinates: [[[170, 80], [175, 80], [175, 85], [170, 85], [170, 80]]] } },
  ] };
  assert.equal(outline.featuresToPaths(fc, [-20, -20, 20, 20], 1000, 380).length, 2);
});

test("a tiny-span view stays inside the viewport — no huge per-vertex coordinates", () => {
  const fc = { type: "FeatureCollection", features: [
    { geometry: { type: "Polygon", coordinates: [[[0, 0], [50, 0], [50, 50], [0, 50], [0, 0]]] } },
  ] };
  const paths = outline.featuresToPaths(fc, [0, 0, 0.001, 0.001], 1000, 380);
  for (const v of paths.flatMap(nums)) assert.ok(v >= -0.01 && v <= 1000.01, `bounded, saw ${v}`);
});

test("ordinary incomplete/unusable geometry is skipped, never thrown on (not total protection)", () => {
  const fc = { type: "FeatureCollection", features: [
    { geometry: null }, {}, { geometry: { type: "Point", coordinates: [0, 0] } },
    { geometry: { type: "Polygon", coordinates: [[[0, 0]]] } },        // ring too short
    { geometry: { type: "Polygon", coordinates: "nope" } },            // container not an array
    { geometry: { type: "Polygon", coordinates: [[[0, 0], [1, 1], null, [2, 2]]] } },   // a bad point
    { geometry: { type: "MultiPolygon", coordinates: [null, [[[0, 0], [1, 0], [1, 1], [0, 0]]]] } },
  ] };
  let paths;
  assert.doesNotThrow(() => { paths = outline.featuresToPaths(fc, outline.WORLD, 1000, 380); });
  for (const v of paths.flatMap(nums)) assert.ok(Number.isFinite(v));
});

test("isUsableOutline requires a non-empty features array; exceedsMax bounds sizes", () => {
  assert.equal(outline.isUsableOutline({ type: "FeatureCollection", features: [{}] }), true);
  assert.equal(outline.isUsableOutline({ type: "FeatureCollection", features: [] }), false);
  assert.equal(outline.isUsableOutline({}), false);
  assert.equal(outline.isUsableOutline(null), false);
  assert.equal(outline.exceedsMax(11, 10), true);
  assert.equal(outline.exceedsMax(10, 10), false);
  assert.equal(outline.exceedsMax(NaN, 10), false);
});

// ── bounded loader (controlled fetch/streams) ──────────────────────────────────────────────────────

// A Response mock that records resource-lifecycle calls, so tests can assert cleanup — that a body we
// stop reading is cancelled, a completed reader is released, and no whole-body read (json/text/arrayBuffer)
// ever happens. `noReader` drops getReader (streaming unavailable); `cancelRejects` makes cancel async-fail.
function makeResponse(chunks, { ok = true, headers = {}, status = 200, noReader = false, cancelRejects = false } = {}) {
  let i = 0;
  const spy = { bodyCancel: 0, readerCancel: 0, readerRelease: 0, json: 0, text: 0, arrayBuffer: 0 };
  const cancelResult = () => (cancelRejects ? Promise.reject(new Error("cancel failed")) : Promise.resolve());
  const reader = {
    read: () => (i < chunks.length ? Promise.resolve({ done: false, value: chunks[i++] }) : Promise.resolve({ done: true })),
    cancel: () => { spy.readerCancel++; return cancelResult(); },
    releaseLock: () => { spy.readerRelease++; },
  };
  const body = { cancel: () => { spy.bodyCancel++; return cancelResult(); } };
  if (!noReader) body.getReader = () => reader;
  const whole = () => Buffer.concat(chunks.map(Buffer.from)).toString("utf8");
  const resp = {
    ok, status,
    headers: { get: (k) => (k in headers ? headers[k] : null) },
    body,
    json: () => { spy.json++; return Promise.resolve(JSON.parse(whole())); },
    text: () => { spy.text++; return Promise.resolve(whole()); },
    arrayBuffer: () => { spy.arrayBuffer++; return Promise.resolve(new ArrayBuffer(0)); },
  };
  return { resp, spy };
}
const wholeBodyReads = (s) => s.json + s.text + s.arrayBuffer;

test("boundedLoad reads a small streamed body, parses it, releases the reader, reads no whole body", async () => {
  const { resp, spy } = makeResponse([enc(JSON.stringify({ type: "FeatureCollection", features: [1, 2] }))]);
  const got = await outline.boundedLoad(() => Promise.resolve(resp), "/x", {}, 1e6);
  assert.deepEqual(got, { type: "FeatureCollection", features: [1, 2] });
  assert.equal(spy.readerRelease, 1, "reader released on EOF");
  assert.equal(wholeBodyReads(spy), 0, "streamed, never json/text/arrayBuffer");
});

test("boundedLoad with no stream reader rejects recoverably WITHOUT reading the whole body", async () => {
  const { resp, spy } = makeResponse([enc('{"type":"FeatureCollection","features":[1]}')], { noReader: true });
  await assert.rejects(outline.boundedLoad(() => Promise.resolve(resp), "/x", {}, 8), (e) => e === "no-stream");
  assert.equal(wholeBodyReads(spy), 0, "no-reader must not fall back to an unbounded whole-body read");
});

test("boundedLoad rejects an oversized Content-Length early and cancels its body", async () => {
  const { resp, spy } = makeResponse([enc("x".repeat(256))], { headers: { "Content-Length": "256" } });
  await assert.rejects(outline.boundedLoad(() => Promise.resolve(resp), "/x", {}, 128), (e) => e === "too-large");
  assert.equal(spy.bodyCancel, 1, "advertised-oversize body cancelled");
  assert.equal(wholeBodyReads(spy), 0);
});

test("boundedLoad rejects a streamed over-budget body and cancels the reader", async () => {
  const big = new Uint8Array(600 * 1024);   // two 600 KB chunks > 1 MB cap
  const { resp, spy } = makeResponse([big, big]);
  await assert.rejects(outline.boundedLoad(() => Promise.resolve(resp), "/x", {}, 1024 * 1024), (e) => e === "too-large");
  assert.equal(spy.readerCancel, 1, "over-budget reader cancelled");
});

test("boundedLoad rejects a non-ok response with its status and discards the body", async () => {
  const { resp, spy } = makeResponse([], { ok: false, status: 503 });
  await assert.rejects(outline.boundedLoad(() => Promise.resolve(resp), "/x", {}, 1e6), (e) => e === 503);
  assert.equal(spy.bodyCancel, 1, "non-ok body discarded");
});

test("a cleanup cancel that rejects neither masks the original result nor goes unhandled", async () => {
  const unhandled = [];
  const onUnhandled = (e) => unhandled.push(e);
  process.on("unhandledRejection", onUnhandled);
  try {
    const big = new Uint8Array(600 * 1024);
    const { resp } = makeResponse([big, big], { cancelRejects: true });   // over-budget AND cancel() async-fails
    await assert.rejects(outline.boundedLoad(() => Promise.resolve(resp), "/x", {}, 1024 * 1024), (e) => e === "too-large");
    await new Promise((r) => setImmediate(r));   // let any stray rejection surface
    assert.equal(unhandled.length, 0, "cancel rejection swallowed, original 'too-large' preserved");
  } finally {
    process.removeListener("unhandledRejection", onUnhandled);
  }
});

// ── real Response / ReadableStream lock-state ────────────────────────────────────────────────────────
// Prove the acquired reader is RELEASED on every terminal path — a cancel() spy can pass while the stream
// stays locked. These use genuine Response/ReadableStream objects and assert body.locked afterward.

test("real Response: normal EOF parses and leaves the stream unlocked", async () => {
  const r = new Response(JSON.stringify({ type: "FeatureCollection", features: [1] }));
  const got = await outline.boundedLoad(() => Promise.resolve(r), "/x", {}, 1e6);
  assert.deepEqual(got, { type: "FeatureCollection", features: [1] });
  assert.equal(r.body.locked, false, "reader released after EOF");
});

test("real Response: an over-budget stream rejects and is left unlocked", async () => {
  const stream = new ReadableStream({ start(c) { c.enqueue(new Uint8Array(2000)); c.close(); } });
  const r = new Response(stream);   // no Content-Length -> exercises the streamed byte-count path
  await assert.rejects(outline.boundedLoad(() => Promise.resolve(r), "/x", {}, 100), (e) => e === "too-large");
  await new Promise((res) => setImmediate(res));
  assert.equal(r.body.locked, false, "reader released after over-budget cancel, not merely cancel-called");
});

test("real Response: an interrupted (errored) read rejects with its reason and is left unlocked", async () => {
  const stream = new ReadableStream({ start(c) { c.error(new Error("boom")); } });
  const r = new Response(stream);
  await assert.rejects(outline.boundedLoad(() => Promise.resolve(r), "/x", {}, 1e6), (e) => e && e.message === "boom");
  await new Promise((res) => setImmediate(res));
  assert.equal(r.body.locked, false, "errored-read path releases the reader (was the missing path)");
});

test("real Response: a source cancel that throws still unlocks and preserves the original reason", async () => {
  const unhandled = [];
  const onU = (e) => unhandled.push(e);
  process.on("unhandledRejection", onU);
  try {
    const stream = new ReadableStream({ start(c) { c.enqueue(new Uint8Array(2000)); }, cancel() { throw new Error("cancel failed"); } });
    const r = new Response(stream);
    await assert.rejects(outline.boundedLoad(() => Promise.resolve(r), "/x", {}, 100), (e) => e === "too-large");
    await new Promise((res) => setImmediate(res));
    assert.equal(r.body.locked, false, "reader released despite a failing source cancel");
    assert.equal(unhandled.length, 0, "cancel failure swallowed, original 'too-large' preserved");
  } finally {
    process.removeListener("unhandledRejection", onU);
  }
});

// ── controller: deadline, stale guard, recovery, admission, area retention ──────────────────────────

function fakeGroup() {
  const kids = [];
  return { get firstChild() { return kids.length ? kids[0] : null; }, appendChild: (e) => kids.push(e), removeChild: () => kids.shift(), count: () => kids.length };
}
function fakeClock() {
  let q = [], id = 0;
  return { set: (fn, ms) => { id++; q.push({ id, fn }); return id; }, clear: (t) => { q = q.filter((x) => x.id !== t); }, tick: () => { const due = q; q = []; due.forEach((x) => x.fn()); } };
}
function fakeAbort() { let a = false; return { signal: { get aborted() { return a; } }, abort: () => { a = true; } }; }
function deferred() { const d = {}; d.promise = new Promise((res, rej) => { d.resolve = res; d.reject = rej; }); return d; }

const SAMPLE = { type: "FeatureCollection", features: [
  { geometry: { type: "Polygon", coordinates: [[[-10, -10], [10, 10], [10, -10], [-10, -10]]] } },
] };

function harness(loadImpl) {
  const clock = fakeClock(), states = [], busy = [];
  let calls = 0;
  const ctl = outline.create({
    group: fakeGroup(), status: { setAttribute() {}, textContent: "" },
    width: 1000, height: 380, timeoutMs: 5000,
    setTimeout: clock.set, clearTimeout: clock.clear, abortController: fakeAbort,
    load: (signal) => { calls++; return loadImpl(signal); },
    setBusy: (on) => busy.push(on), onState: (s) => states.push(s),
  });
  return { ctl, clock, states, busy, calls: () => calls };
}

test("a held load hits the deadline and shows a recoverable error", async () => {
  const held = deferred();
  const h = harness(() => held.promise);
  h.ctl.open();
  await flush();
  h.clock.tick();                          // deadline elapses
  assert.equal(h.states.at(-1), "error");
  assert.equal(h.busy.at(-1), false, "busy cleared on timeout");
});

test("a load that arrives AFTER its deadline cannot overwrite the timeout", async () => {
  const held = deferred();
  const h = harness(() => held.promise);
  h.ctl.open();
  await flush();
  h.clock.tick();                          // -> error
  held.resolve(SAMPLE);                     // late success for the timed-out attempt
  await flush();
  assert.equal(h.states.at(-1), "error", "stale success ignored");
  assert.equal(h.ctl.state().loaded, false, "no data adopted from the stale load");
});

test("an ordinary load failure shows a recoverable error, and Retry then succeeds", async () => {
  let mode = "fail";
  const h = harness(() => (mode === "fail" ? Promise.reject("boom") : Promise.resolve(SAMPLE)));
  h.ctl.open();
  await flush();
  assert.equal(h.states.at(-1), "error");
  mode = "ok";
  h.ctl.retry();
  await flush();
  assert.equal(h.states.at(-1), "ready");
  assert.equal(h.ctl.state().loaded, true);
});

test("an empty features array is treated as recoverable unavailable, not ready", async () => {
  const h = harness(() => Promise.resolve({ type: "FeatureCollection", features: [] }));
  h.ctl.open();
  await flush();
  assert.equal(h.states.at(-1), "unavailable");
  assert.equal(h.ctl.state().loaded, false);
});

test("a payload that yields zero drawable paths is unavailable, not a blank 'ready' map", async () => {
  const h = harness(() => Promise.resolve({ type: "FeatureCollection", features: [{ geometry: { type: "Point", coordinates: [0, 0] } }] }));
  h.ctl.open();
  await flush();
  assert.equal(h.states.at(-1), "unavailable");
});

test("pressing World after a bad area clears the badbbox state even though paths are reused", async () => {
  const h = harness(() => Promise.resolve(SAMPLE));
  h.ctl.open();
  await flush();
  assert.equal(h.states.at(-1), "ready");
  h.ctl.area([60, -12, 35, 30]);            // inverted -> badbbox
  assert.equal(h.states.at(-1), "badbbox");
  h.ctl.reset();                            // World: unchanged view, but must clear the error
  assert.equal(h.states.at(-1), "ready");
});

test("an area requested during load is retained and drawn when the load lands (not silently World)", async () => {
  const held = deferred();
  const h = harness(() => held.promise);
  h.ctl.area([35, -12, 60, 30]);            // requested before data exists
  await flush();
  held.resolve(SAMPLE);
  await flush();
  assert.deepEqual(h.ctl.state().bbox, [35, -12, 60, 30], "the requested area was drawn, not World");
});

test("the outline is fetched once across repeated opens and cache reuse", async () => {
  const h = harness(() => Promise.resolve(SAMPLE));
  h.ctl.open(); h.ctl.open();               // second open during load must not start a new request
  await flush();
  h.ctl.open();                             // reuse after load: still no new request
  await flush();
  assert.equal(h.calls(), 1);
});
