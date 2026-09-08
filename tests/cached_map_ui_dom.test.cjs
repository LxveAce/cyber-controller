/* Exercise the cached-map consumer with its actual renderer, a controlled loader,
 * a small DOM fixture and a simulated ResizeObserver. No browser or network is used. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("fs"), path = require("path");

const RENDERER_PATH = path.join(__dirname, "..", "src", "ui", "web", "static", "map_raster.js");
const REFORM_PATH = path.join(__dirname, "..", "src", "ui", "web", "static", "reform.js");
const R = require(RENDERER_PATH);

const SRC = fs.readFileSync(REFORM_PATH, "utf8");
function slice(startsWith, endStartsWith) {
  const lines = SRC.split(/\r?\n/);
  const s = lines.findIndex((l) => l.startsWith(startsWith));
  const e = lines.findIndex((l, i) => i > s && l.startsWith(endStartsWith));
  assert.ok(s >= 0 && e > s, "located block " + startsWith);
  return lines.slice(s, e).join("\n");
}
const wireBtnSrc = slice("  function wireBtn(id, fn)", "  wireBtn(\"dash-connect\"");
const initSrc = slice("  // ── MAP ▸ Offline maps ▸ Cached tiles mode", "  // The shared desktop/browser view currently supports");

// ── minimal DOM / window / ResizeObserver / controlled loader ──────────────────────────────────────
function harness(ignoreAbort = false) {
  function el(tag) {
    return {
      tagName: (tag || "DIV").toUpperCase(), style: {}, options: [], _kids: [], _h: {}, dataset: {},
      hidden: false, value: "", textContent: "",
      clientWidth: 0, clientHeight: 0,
      addEventListener(t, fn) { (this._h[t] = this._h[t] || []).push(fn); },
      click() { (this._h.click || []).forEach((fn) => fn({ target: this })); },
      setAttribute(k, v) { this["_a_" + k] = v; }, getAttribute(k) { return this["_a_" + k]; },
      appendChild(c) { this._kids.push(c); if (c.tagName === "OPTION") this.options.push(c); return c; },
      removeChild(c) { const i = this._kids.indexOf(c); if (i >= 0) this._kids.splice(i, 1); return c; },
      get firstChild() { return this._kids.length ? this._kids[0] : null; },
      querySelectorAll() { return []; },
      classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    };
  }
  const ids = ["rt-view", "rt-provider", "rt-bbox", "rt-show", "rt-stop", "rt-reset", "rt-msg", "rt-attr", "mapo-mode-coarse", "mapo-mode-raster"];
  const els = {}; ids.forEach((id) => { els[id] = el(); els[id].id = id; });
  const coarsePane = el(), rasterPane = el();
  coarsePane.dataset.mode = "coarse"; rasterPane.dataset.mode = "raster";
  const document = {
    getElementById: (id) => els[id] || null,
    createElement: (t) => el(t),
    querySelector: (sel) => (sel.indexOf("coarse") >= 0 ? coarsePane : sel.indexOf("raster") >= 0 ? rasterPane : null),
  };
  // controlled loader: loadTile returns a held promise; releaseAll() settles them "ok"
  let fetchCount = 0, completedCount = 0; const pending = [];
  const window = {
    CCMapRaster: R,
    CCMapRasterLoader: {
      createLoader: () => ({
        loadTile(provider, z, x, y, signal) {
          fetchCount++;
          return new Promise((res) => {
            const rec = { res, z, x, y, done: false };
            pending.push(rec);
            if (!ignoreAbort && signal && signal.addEventListener) signal.addEventListener("abort", () => { if (!rec.done) { rec.done = true; res({ status: "aborted" }); } });
          });
        },
      }),
    },
  };
  // simulated ResizeObserver: observe() stores the callback; fireResize() invokes them
  const roCbs = [];
  function ResizeObserver(cb) { this.observe = () => roCbs.push(cb); this.disconnect = () => {}; }
  const flush = () => new Promise((r) => setImmediate(r));
  function releaseAll() { pending.splice(0).forEach((p) => { if (!p.done) { p.done = true; completedCount++; p.res({ status: "ok", url: "data:tile/" + p.z + "/" + p.x + "/" + p.y }); } }); }

  // run the real wireBtn + initCachedTiles in this stubbed scope
  // eslint-disable-next-line no-new-func
  new Function("document", "window", "ResizeObserver", wireBtnSrc + "\n" + initSrc)(document, window, ResizeObserver);

  return { els, coarsePane, rasterPane, fireResize: () => roCbs.forEach((cb) => cb([], null)), releaseAll, flush,
           fetchCount: () => fetchCount, completedCount: () => completedCount, pendingCount: () => pending.filter(p => !p.done).length, imgs: () => els["rt-view"]._kids };
}

const BBOX = "40.55,-74.05,40.90,-73.75";
function expectedPx(w, h, zoom) {
  const plan = R.planView({ bbox: [40.55, -74.05, 40.90, -73.75], viewport: { width: w, height: h }, provider: "carto-dark", zoom });
  return plan.tiles.map((t) => t.px.left + "|" + t.px.top + "|" + t.px.size).sort();
}

test("DOM lifecycle: a tile arriving AFTER a mid-load resize lands at the current geometry, no new requests", async () => {
  const H = harness();
  H.els["rt-view"].clientWidth = 742; H.els["rt-view"].clientHeight = 285;   // ~1000-wide viewport
  H.els["mapo-mode-raster"].click();
  H.els["rt-bbox"].value = BBOX;
  H.els["rt-show"].click();                       // plan at 742; loads start (held)
  await H.flush();
  const fetchesAtShow = H.fetchCount();
  assert.ok(fetchesAtShow > 0, "requests issued after Show");
  const zoom = R.planView({ bbox: [40.55, -74.05, 40.90, -73.75], viewport: { width: 742, height: 285 }, provider: "carto-dark" }).zoom;

  // resize BEFORE any tile settles: no images yet, observer fires refit (nothing to reposition)
  H.els["rt-view"].clientWidth = 442; H.els["rt-view"].clientHeight = 285;
  H.fireResize();
  await H.flush();

  H.releaseAll(); await H.flush(); await H.flush();   // late tiles arrive AFTER the resize
  const painted = H.imgs().map((im) => im.style.left + "|" + im.style.top + "|" + im.style.width)
    .map((s) => s.replace(/px/g, "")).sort();
  assert.ok(painted.length > 0, "tiles painted");
  assert.deepEqual(painted, expectedPx(442, 285, zoom), "late tiles at the CURRENT (442) same-zoom geometry, not the stale 742 plan");
  assert.equal(H.fetchCount(), fetchesAtShow, "the resize/refit issued NO new tile requests");
  assert.equal(H.els["rt-msg"].textContent, "Complete — all " + painted.length + " tiles cached.");
});

test("DOM lifecycle: leaving an in-flight load settles 'Stopped.'; a late stale completion can't change it", async () => {
  const H = harness(true);   // this loader deliberately ignores abort and succeeds late
  H.els["rt-view"].clientWidth = 742; H.els["rt-view"].clientHeight = 285;
  H.els["mapo-mode-raster"].click();
  H.els["rt-bbox"].value = BBOX;
  H.els["rt-show"].click();
  await H.flush();
  assert.match(H.els["rt-msg"].textContent, /Reading cache/, "in progress");
  assert.ok(H.pendingCount() > 0, "held loads exist before cancellation");
  H.els["mapo-mode-coarse"].click();              // leave the active load
  assert.equal(H.els["rt-msg"].textContent, "Stopped.", "leaving an in-flight load settles Stopped.");
  H.releaseAll(); await H.flush(); await H.flush();   // held loads complete late (stale)
  assert.ok(H.completedCount() > 0, "held requests actually completed successfully after cancellation");
  assert.equal(H.imgs().length, 0, "late successes do not paint stale tiles");
  assert.equal(H.els["rt-msg"].textContent, "Stopped.", "a late stale completion cannot change the settled status");
});
