/* Tests for the explicit Web-Mercator raster tile-layout model. The projection is asserted to mirror
 * src/core/map_tiles.py (values cross-checked against the Python), with high-latitude / pole / world-boundary
 * and wrapping/invalid input; planning is bounded before enumeration; the cache-only viewer is driven with a
 * stubbed loader (no network, no real tiles). Pure/inert — no DOM, no app, no I/O beyond require. */
const test = require("node:test"), assert = require("node:assert/strict");
const R = require("../src/ui/web/static/map_raster.js");

const flush = () => new Promise((res) => setImmediate(res));

// Re-derive the map_tiles.py slippy formula here so the test proves map_raster reproduces it (no drift).
function refFrac(lat, lon, z) {
  const n = 2 ** z;
  const la = Math.max(-R.MERC_LAT_LIMIT, Math.min(R.MERC_LAT_LIMIT, lat));
  return [(lon + 180) / 360 * n, (1 - Math.asinh(Math.tan(la * Math.PI / 180)) / Math.PI) / 2 * n];
}

// ── projection mirrors map_tiles.py ──────────────────────────────────────────────────────────────

test("lonLatToTileFrac / tileXY reproduce the map_tiles slippy formula (values verified vs Python)", () => {
  // absolute reference values computed from map_tiles.py:
  const cases = [
    [0, 0, 1, [1, 1]], [51.5, -0.12, 12, [2046, 1362]], [85.05, 180, 3, [7, 0]],
    [-85.05, -180, 5, [0, 31]], [40.7128, -74.006, 14, [4823, 6160]], [89, 10, 4, [8, 0]],
  ];
  for (const [lat, lon, z, tile] of cases) {
    const f = R.lonLatToTileFrac(lat, lon, z), rf = refFrac(lat, lon, z);
    assert.ok(Math.abs(f[0] - rf[0]) < 1e-9 && Math.abs(f[1] - rf[1]) < 1e-9, `frac ${lat},${lon},z${z}`);
    assert.deepEqual(R.tileXY(lat, lon, z), tile, `tile ${lat},${lon},z${z}`);
  }
});

test("constants match map_tiles.py", () => {
  assert.equal(R.WORLD_PX, 40075016);
  assert.equal(R.TILE_SIZE, 256);
  assert.equal(R.MIN_ZOOM, 0); assert.equal(R.MAX_ZOOM, 19);
  assert.equal(R.MERC_LAT_LIMIT, 85.05112878);
  assert.equal(R.DEFAULT_PROVIDER, "carto-dark");
});

test("high-latitude alignment: mercator compresses toward the poles, clamped at the limit", () => {
  const [, ySouthEdge] = R.lonLatToWorld(R.MERC_LAT_LIMIT, 0);   // top of the world square
  const [, yNorthEdge] = R.lonLatToWorld(-R.MERC_LAT_LIMIT, 0);  // bottom
  assert.ok(Math.abs(ySouthEdge - 0) < 1, "max latitude -> y≈0 (top)");
  assert.ok(Math.abs(yNorthEdge - R.WORLD_PX) < 1, "min latitude -> y≈WORLD_PX (bottom)");
  // y strictly decreases as latitude rises
  const ys = [-80, -40, 0, 40, 80].map((lat) => R.lonLatToWorld(lat, 0)[1]);
  for (let i = 1; i < ys.length; i++) assert.ok(ys[i] < ys[i - 1], "monotonic");
  // beyond the mercator limit is clamped, not infinite
  assert.deepEqual(R.lonLatToWorld(90, 0), R.lonLatToWorld(R.MERC_LAT_LIMIT, 0), "lat 90 clamps to the limit");
  for (const v of R.lonLatToWorld(90, 180)) assert.ok(Number.isFinite(v), "finite at the pole/boundary");
});

test("world-boundary longitudes map to the world-square edges", () => {
  assert.ok(Math.abs(R.lonLatToWorld(0, -180)[0] - 0) < 1e-6, "lon -180 -> x=0");
  assert.ok(Math.abs(R.lonLatToWorld(0, 180)[0] - R.WORLD_PX) < 1e-6, "lon 180 -> x=WORLD_PX");
});

test("validateBbox rejects wrapping/inverted/out-of-range/non-finite; accepts an ordered area", () => {
  assert.deepEqual(R.validateBbox([35, -12, 60, 30]), [35, -12, 60, 30]);
  assert.equal(R.validateBbox([35, 30, 60, -12]), null, "wrapping lon (W>E)");
  assert.equal(R.validateBbox([60, -12, 35, 30]), null, "inverted lat (S>N)");
  assert.equal(R.validateBbox([35, -190, 60, 30]), null, "lon out of range");
  assert.equal(R.validateBbox([-95, -12, 60, 30]), null, "lat out of range");
  assert.equal(R.validateBbox([35, -12, NaN, 30]), null, "non-finite");
  assert.equal(R.validateBbox([35, -12, 60]), null, "wrong length");
});

// ── providers ─────────────────────────────────────────────────────────────────────────────────────

test("providerList exposes the 5 code-defined providers + default, with attribution and NO urls", () => {
  const pl = R.providerList();
  assert.equal(pl.default, "carto-dark");
  const ids = pl.providers.map((p) => p.id).sort();
  assert.deepEqual(ids, ["carto-dark", "carto-light", "carto-voyager", "osm", "osm-de"]);
  for (const p of pl.providers) {
    assert.ok(p.label && p.attribution && p.max_zoom === 19, "max_zoom matches the wire contract field name");
    assert.ok(/OpenStreetMap/.test(p.attribution), "OSM credited");
    assert.equal(p.url, undefined); assert.equal(p.urlTemplate, undefined);   // no upstream URL exposed
  }
  assert.equal(pl.providers.find((p) => p.id === "carto-dark").attribution, "© OpenStreetMap contributors © CARTO");
});

test("getProvider is strict — an unknown id returns null (no silent fallback like map_tiles.get_provider)", () => {
  assert.equal(R.getProvider("carto-dark").id, "carto-dark");
  assert.equal(R.getProvider("CARTO-DARK").id, "carto-dark", "case-insensitive");
  assert.equal(R.getProvider("nope"), null);
  assert.equal(R.getProvider(""), null);
  assert.equal(R.getProvider(null), null);
});

// ── bounded planning ────────────────────────────────────────────────────────────────────────────

const VP = { width: 800, height: 600 };

test("planView returns a bounded, finite tile set with pixel rects for an ordinary view", () => {
  const plan = R.planView({ bbox: [40.70, -74.02, 40.75, -73.96], viewport: VP, provider: "carto-dark" });
  assert.equal(plan.ok, true);
  assert.ok(plan.tiles.length > 0 && plan.tiles.length <= R.MAX_TILES_PER_VIEW);
  assert.equal(plan.provider, "carto-dark");
  assert.equal(plan.attribution, "© OpenStreetMap contributors © CARTO");
  assert.ok(Number.isInteger(plan.zoom) && plan.zoom >= 0 && plan.zoom <= 19);
  for (const t of plan.tiles) {
    assert.ok(Number.isInteger(t.x) && Number.isInteger(t.y) && t.x >= 0 && t.y >= 0);
    for (const v of [t.px.left, t.px.top, t.px.size]) assert.ok(Number.isFinite(v));
    assert.ok(t.px.size > 0);
  }
});

test("planning is bounded BEFORE enumeration: a fixed dense zoom is rejected, auto zoom is lowered to fit", () => {
  const world = [-85, -179, 85, 179];
  // explicit high zoom over the whole world would be billions of tiles -> rejected, never enumerated
  const dense = R.planView({ bbox: world, viewport: VP, provider: "osm", zoom: 12 });
  assert.equal(dense.ok, false); assert.equal(dense.reason, "too-many-tiles");
  // with no explicit zoom, the scale-matched zoom is lowered until <= 64 tiles
  const auto = R.planView({ bbox: world, viewport: VP, provider: "osm" });
  assert.equal(auto.ok, true);
  assert.ok(auto.tiles.length <= R.MAX_TILES_PER_VIEW, `<=64 tiles, got ${auto.tiles.length}`);
});

test("planView rejects bad inputs clearly", () => {
  assert.equal(R.planView({ bbox: [40.7, -74.0, 40.75, -73.9], viewport: VP, provider: "nope" }).reason, "unknown-provider");
  assert.equal(R.planView({ bbox: [40.7, -74, 40.75, -73.9], viewport: { width: 0, height: 6 }, provider: "osm" }).reason, "invalid-viewport");
  assert.equal(R.planView({ bbox: [60, 30, 35, -12], viewport: VP, provider: "osm" }).reason, "invalid-bbox");
  assert.equal(R.planView({ bbox: [40.7, -74, 40.75, -73.9], viewport: VP, provider: "osm", zoom: 3.5 }).reason, "invalid-zoom");
  assert.equal(R.planView({ bbox: [40.7, -74, 40.75, -73.9], viewport: VP, provider: "osm", zoom: 25 }).reason, "invalid-zoom");
});

// ── cache-only viewer: concurrency / missing / cancel / no-retry ─────────────────────────────────

function tinyPlan(nTiles) {
  const tiles = [];
  for (let i = 0; i < nTiles; i++) tiles.push({ z: 4, x: i, y: 0, px: { left: i * 10, top: 0, size: 10 } });
  return { ok: true, provider: "osm", attribution: "© OpenStreetMap contributors", zoom: 4, tiles };
}

test("viewer honours bounded concurrency (<=4) and reports complete when all tiles are cached", async () => {
  let active = 0, peak = 0, calls = 0;
  const load = () => { calls++; active++; peak = Math.max(peak, active); return new Promise((res) => setImmediate(() => { active--; res("blob:url"); })); };
  const painted = [];
  const v = R.createViewer({ load, concurrency: 4, onTile: (t) => painted.push(t) });
  const res = await v.show(tinyPlan(20));
  assert.ok(peak <= 4, `peak concurrency ${peak} <= 4`);
  assert.equal(calls, 20); assert.equal(res.loaded, 20); assert.equal(res.missing, 0);
  assert.equal(painted.length, 20);
});

test("viewer reports missing tiles as blank and never retries a failed load", async () => {
  let calls = 0;
  const load = (p, z, x) => { calls++; return Promise.resolve(x % 2 === 0 ? "blob:url" : null); };  // odd tiles missing
  const statuses = [];
  const v = R.createViewer({ load, onStatus: (s) => statuses.push(s.state) });
  const res = await v.show(tinyPlan(10));
  assert.equal(res.loaded, 5); assert.equal(res.missing, 5);
  assert.equal(calls, 10, "each tile loaded exactly once — no retry");
});

test("a partially-cached view RESTS in 'partial', not 'complete' (honest terminal status)", async () => {
  // 8 of 12 cached -> the final resting state must be 'partial'; a consumer keying on it must not read a
  // partial cache as fully cached.
  const load = (p, z, x) => Promise.resolve(x < 8 ? "blob:url" : null);   // 8 cached, 4 missing
  const statuses = [];
  const res = await R.createViewer({ load, onStatus: (s) => statuses.push(s.state) }).show(tinyPlan(12));
  assert.equal(res.loaded, 8); assert.equal(res.missing, 4);
  assert.equal(statuses.at(-1), "partial", "terminal state is partial, not complete");
});

test("a fully-cached view rests in 'complete'; an all-missing view rests in 'empty'", async () => {
  const done = [];
  await R.createViewer({ load: () => Promise.resolve("blob:url"), onStatus: (s) => done.push(s.state) }).show(tinyPlan(6));
  assert.equal(done.at(-1), "complete");
  const empty = [];
  const res = await R.createViewer({ load: () => Promise.resolve(null), onStatus: (s) => empty.push(s.state) }).show(tinyPlan(6));
  assert.equal(res.loaded, 0); assert.equal(empty.at(-1), "empty");
});

test("a rejected load counts once as missing, no unhandled rejection", async () => {
  let calls = 0;
  const load = () => { calls++; return Promise.reject(new Error("io")); };
  const v = R.createViewer({ load });
  const res = await v.show(tinyPlan(4));
  assert.equal(calls, 4); assert.equal(res.missing, 4); assert.equal(res.loaded, 0);
});

test("cancel/stale guard: a superseded run neither paints nor reports complete", async () => {
  const painted = [], statuses = [];
  const load = () => new Promise((res) => setImmediate(() => res("blob:url")));
  const v = R.createViewer({ load, onTile: () => painted.push(1), onStatus: (s) => statuses.push(s.state) });
  const p = v.show(tinyPlan(8));   // loads are in-flight (settle on setImmediate)
  v.cancel();                       // supersede before any load resolves
  await p; await flush();
  assert.equal(painted.length, 0, "no tiles painted after cancel");
  assert.ok(!statuses.includes("complete"), "no 'complete' reported for the cancelled run");
});

// ── contain-fit, hard tile cap, supersession settlement, cross-run cap, cooperative abort ──

test("contain-fit: a tall selected area fits BOTH viewport axes and is centered", () => {
  const vp = { width: 800, height: 400 };
  const plan = R.planView({ bbox: [-60, -10, 60, 10], viewport: vp, zoom: 2 });
  assert.equal(plan.ok, true);
  const a = plan.area;   // the selected area's pixel rectangle
  assert.ok(a.left >= -0.1 && a.top >= -0.1, "top-left corner within view");
  assert.ok(a.left + a.width <= vp.width + 0.1 && a.top + a.height <= vp.height + 0.1, "bottom-right corner within view");
  assert.ok(a.height <= vp.height + 0.1, `area height ${a.height} fits`);
  assert.ok(Math.abs(a.left - (vp.width - a.width) / 2) < 0.5 && Math.abs(a.top - (vp.height - a.height) / 2) < 0.5, "centered");
  // derive the uniform scale from a tile (root's oracle shape) and confirm the projected height fits
  const scale = plan.tiles[0].px.size / R.tileWorldRect(0, 0, plan.zoom).size;
  assert.ok((plan.worldRect.wy1 - plan.worldRect.wy0) * scale <= vp.height + 0.1, "projected area height <= viewport height");
});

test("64 is the hard tile ceiling regardless of a larger maxTiles; invalid budgets reject", () => {
  const vp = { width: 800, height: 400 };
  const capped = R.planView({ bbox: [-85, -180, 85, 180], viewport: vp, zoom: 4, maxTiles: 300 });
  assert.ok(!capped.ok || capped.tiles.length <= 64, "a 300 preference cannot exceed 64");
  assert.equal(capped.ok, false); assert.equal(capped.reason, "too-many-tiles");
  assert.equal(R.planView({ bbox: [-10, -10, 10, 10], viewport: vp, maxTiles: 0 }).reason, "invalid-max-tiles");
  assert.equal(R.planView({ bbox: [-10, -10, 10, 10], viewport: vp, maxTiles: 3.5 }).reason, "invalid-max-tiles");
  const small = R.planView({ bbox: [40.70, -74.02, 40.75, -73.96], viewport: vp, maxTiles: 4 });
  assert.ok(small.ok && small.tiles.length <= 4, "a smaller-than-64 preference is still honoured");
});

test("a fractional maxZoom yields an integral planned zoom (never fractional)", () => {
  const plan = R.planView({ bbox: [-85, -180, 85, 180], viewport: { width: 800, height: 400 }, maxZoom: 2.5 });
  assert.ok(!plan.ok || Number.isInteger(plan.zoom));
});

function mkPlan(n) { return { ok: true, provider: "carto-dark", tiles: Array.from({ length: n }, (_, i) => ({ z: 3, x: i, y: 0, px: { left: i * 10, top: 0, size: 10 } })) }; }
function heldLoader() {
  const releases = []; let active = 0, peak = 0;
  const load = () => new Promise((resolve) => { active++; peak = Math.max(peak, active); releases.push(() => { active--; resolve(null); }); });
  return { load, releaseAll: () => releases.splice(0).forEach((f) => f()), peak: () => peak, outstanding: () => active };
}
const microTick = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };

test("superseding a held load settles the previous show() immediately (not waiting for the loader)", async () => {
  const h = heldLoader();
  const v = R.createViewer({ load: h.load });
  let firstDone = false;
  const first = v.show(mkPlan(1)).then((r) => { firstDone = true; return r; });
  await microTick();
  const second = v.show(mkPlan(0));   // an empty view supersedes the held first run
  await microTick();
  assert.equal(firstDone, true, "the held first show resolved on supersession");
  assert.equal((await first).cancelled, true);
  assert.equal((await second).cancelled, false); assert.equal((await second).total, 0);
  h.releaseAll();
});

test("the outstanding cap (4) holds across cancel+show, and stale results never paint or alter counters", async () => {
  const h = heldLoader(); let painted = 0;
  const v = R.createViewer({ load: h.load, onTile: () => painted++ });
  const old = v.show(mkPlan(4)); await microTick();
  assert.equal(h.peak(), 4, "first run holds four");
  v.cancel();
  const next = v.show(mkPlan(4)); await microTick();
  assert.ok(h.peak() <= 4, `never more than four outstanding across replacement, peak ${h.peak()}`);
  assert.equal(painted, 0, "held/stale loads never painted");
  while (h.outstanding() > 0) { h.releaseAll(); await microTick(); }
  await Promise.all([old, next]);
  assert.ok(h.peak() <= 4, "still <=4 after draining");
});

test("a cooperative aborting loader frees capacity on supersession (old ops actually end)", async () => {
  let started = 0, aborted = 0;
  const load = (p, z, x, y, signal) => new Promise((resolve) => {
    started++;
    if (signal) signal.addEventListener("abort", () => { aborted++; resolve(null); });
  });
  const v = R.createViewer({ load });
  const r1 = v.show(mkPlan(4)); await microTick();
  assert.equal(started, 4);
  v.cancel();                          // aborts the four in-flight ops
  await microTick();
  assert.equal(aborted, 4, "all four outstanding ops received abort and ended");
  assert.equal(v.outstanding(), 0, "capacity freed after cooperative abort");
  const r2 = v.show(mkPlan(4)); await microTick();
  v.cancel(); await microTick();
  await Promise.all([r1, r2]);
});
