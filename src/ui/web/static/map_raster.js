/* Offline raster basemap — an EXPLICIT Web-Mercator tile-layout model for a future cache-only viewer.
 *
 * This is deliberately its own web-mercator mode: the shipped web overlay (reform.js toXY / map_outline.js
 * project) is linear equirectangular, but XYZ raster tiles are web-mercator, so they cannot share that plane.
 * The math here mirrors src/core/map_tiles.py exactly (WORLD_PX, TILE_SIZE, zoom range, the mercator lat
 * limit, and the asinh(tan) projection) so a rendered tile lands where the cache says it should. Pure model:
 * it plans which tiles a view needs and where each one sits in pixels; it issues no request and reads no tile
 * bytes. createViewer() is a cache-only preview controller that loads planned tiles through an injected
 * loader with bounded concurrency — no downloads, no retries, no network of its own. */
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.CCMapRaster = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // ── constants (mirror map_tiles.py) ──────────────────────────────────────────────────────────────
  var TILE_SIZE = 256;
  var WORLD_PX = 40075016.0;          // web-mercator world square edge in the shared world_px plane
  var MIN_ZOOM = 0, MAX_ZOOM = 19;
  var MERC_LAT_LIMIT = 85.05112878;   // latitude beyond which web-mercator is undefined; clamp to it
  var DEFAULT_PROVIDER = "carto-dark";
  var MAX_TILES_PER_VIEW = 64;        // a view never plans more than this; 8x8 is far above any real viewport

  // Code-defined providers (id/label/attribution/max_zoom) mirrored from map_tiles.py PROVIDERS. URLs are
  // intentionally NOT exposed here — the operator picks a name; the backend route owns the upstream URL.
  var CARTO_ATTR = "© OpenStreetMap contributors © CARTO";
  var OSM_ATTR = "© OpenStreetMap contributors";
  var PROVIDERS = {
    "carto-dark": { id: "carto-dark", label: "CARTO Dark Matter", attribution: CARTO_ATTR, max_zoom: 19 },
    "carto-light": { id: "carto-light", label: "CARTO Positron", attribution: CARTO_ATTR, max_zoom: 19 },
    "carto-voyager": { id: "carto-voyager", label: "CARTO Voyager", attribution: CARTO_ATTR, max_zoom: 19 },
    "osm": { id: "osm", label: "OpenStreetMap", attribution: OSM_ATTR, max_zoom: 19 },
    "osm-de": { id: "osm-de", label: "OpenStreetMap (DE)", attribution: OSM_ATTR, max_zoom: 19 }
  };

  function finiteNum(n) { return typeof n === "number" && isFinite(n); }
  function isInt(n) { return finiteNum(n) && Math.floor(n) === n; }

  // Strict provider resolution — UNLIKE map_tiles.get_provider, an unknown id returns null (no silent
  // fallback), so the route/model rejects it (404) instead of quietly serving the default.
  function getProvider(id) {
    if (typeof id !== "string") return null;
    var p = PROVIDERS[id.trim().toLowerCase()];
    return p || null;
  }
  function providerList() {
    return { default: DEFAULT_PROVIDER, providers: Object.keys(PROVIDERS).map(function (k) {
      var p = PROVIDERS[k]; return { id: p.id, label: p.label, attribution: p.attribution, max_zoom: p.max_zoom };
    }) };
  }

  // ── pure web-mercator tile math (mirrors map_tiles.py) ────────────────────────────────────────────
  function clampZoom(z, maxZoom) {
    if (maxZoom == null) maxZoom = MAX_ZOOM;
    return Math.max(MIN_ZOOM, Math.min(Math.trunc(z), maxZoom));
  }
  function clampMercLat(lat) { return Math.max(-MERC_LAT_LIMIT, Math.min(MERC_LAT_LIMIT, lat)); }

  // Normalized web-mercator of (lat, lon) scaled into [0, WORLD_PX]. y grows south (north at top).
  function lonLatToWorld(lat, lon) {
    var la = clampMercLat(lat), latRad = la * Math.PI / 180;
    var nx = (lon + 180) / 360;
    var ny = (1 - Math.asinh(Math.tan(latRad)) / Math.PI) / 2;
    return [nx * WORLD_PX, ny * WORLD_PX];
  }
  // Fractional tile coords at zoom z (integer part = tile index). Same formula as lonlat_to_tile_frac.
  function lonLatToTileFrac(lat, lon, z) {
    z = clampZoom(z);
    var n = Math.pow(2, z), la = clampMercLat(lat), latRad = la * Math.PI / 180;
    return [(lon + 180) / 360 * n, (1 - Math.asinh(Math.tan(latRad)) / Math.PI) / 2 * n];
  }
  function tileXY(lat, lon, z) {
    z = clampZoom(z);
    var n = Math.pow(2, z), f = lonLatToTileFrac(lat, lon, z);
    return [Math.min(n - 1, Math.max(0, Math.floor(f[0]))), Math.min(n - 1, Math.max(0, Math.floor(f[1])))];
  }
  function tileWorldRect(x, y, z) {
    z = clampZoom(z);
    var size = WORLD_PX / Math.pow(2, z);
    return { wx: x * size, wy: y * size, size: size };
  }
  function zoomForWorldPerPx(worldPerPx, maxZoom) {
    if (maxZoom == null) maxZoom = MAX_ZOOM;
    if (!(worldPerPx > 0) || !isFinite(worldPerPx)) return clampZoom(maxZoom, maxZoom);
    return clampZoom(Math.round(Math.log2(WORLD_PX / (TILE_SIZE * worldPerPx))), maxZoom);
  }

  function clampIdx(i, n) { return Math.max(0, Math.min(n - 1, i)); }
  // Inclusive tile-index range covering a world box at zoom z — arithmetic only, NO enumeration.
  function tileRange(wx0, wy0, wx1, wy1, z) {
    var n = Math.pow(2, z), size = WORLD_PX / n;
    var lox = Math.min(wx0, wx1), hix = Math.max(wx0, wx1);
    var loy = Math.min(wy0, wy1), hiy = Math.max(wy0, wy1);
    var x0 = clampIdx(Math.floor(lox / size), n), x1 = clampIdx(Math.floor(hix / size), n);
    var y0 = clampIdx(Math.floor(loy / size), n), y1 = clampIdx(Math.floor(hiy / size), n);
    return { x0: x0, x1: x1, y0: y0, y1: y1, nx: x1 - x0 + 1, ny: y1 - y0 + 1 };
  }

  // Validate an explicit [S,W,N,E] area for a mercator view: finite, ordered (S<N, W<E — a wrapping box is
  // rejected, not guessed), lon in [-180,180], lat in [-90,90] (projection clamps to the mercator limit).
  function validateBbox(b) {
    if (!Array.isArray(b) || b.length !== 4 || !b.every(finiteNum)) return null;
    var s = b[0], w = b[1], n = b[2], e = b[3];
    if (s < -90 || n > 90 || w < -180 || e > 180) return null;
    if (!(s < n) || !(w < e)) return null;
    return [s, w, n, e];
  }

  // Plan the tiles a view needs, bounded BEFORE enumeration. opts:
  //   { bbox:[S,W,N,E], viewport:{width,height}, provider, zoom?, maxTiles?, maxZoom? }
  // Returns { ok:true, provider, attribution, zoom, tiles:[{z,x,y,px:{left,top,size}}], worldRect } or
  //   { ok:false, reason }. With an explicit `zoom`, a view needing more than maxTiles is REJECTED
  //   ("too-many-tiles"); with no zoom, the scale-matched zoom is lowered until the count fits.
  function planView(opts) {
    opts = opts || {};
    // 64 is the HARD tile ceiling for any view; a larger caller preference is clamped, and a non-integral or
    // sub-1 budget is rejected — the count is still bounded before enumeration.
    var maxTiles = MAX_TILES_PER_VIEW;
    if (opts.maxTiles != null) {
      if (!isInt(opts.maxTiles) || opts.maxTiles < 1) return { ok: false, reason: "invalid-max-tiles" };
      maxTiles = Math.min(opts.maxTiles, MAX_TILES_PER_VIEW);
    }
    var prov = getProvider(opts.provider || DEFAULT_PROVIDER);
    if (!prov) return { ok: false, reason: "unknown-provider" };
    var vp = opts.viewport;
    if (!vp || !(vp.width > 0) || !(vp.height > 0) || !finiteNum(vp.width) || !finiteNum(vp.height)) {
      return { ok: false, reason: "invalid-viewport" };
    }
    var b = validateBbox(opts.bbox);
    if (!b) return { ok: false, reason: "invalid-bbox" };
    // The zoom ceiling is an integer, so a fractional maxZoom can never yield a fractional planned zoom.
    var maxZoom = Math.floor(Math.min(finiteNum(opts.maxZoom) ? opts.maxZoom : prov.max_zoom, prov.max_zoom));

    var sw = lonLatToWorld(b[0], b[1]), ne = lonLatToWorld(b[2], b[3]);
    var wx0 = Math.min(sw[0], ne[0]), wx1 = Math.max(sw[0], ne[0]);
    var wy0 = Math.min(sw[1], ne[1]), wy1 = Math.max(sw[1], ne[1]);
    var worldW = wx1 - wx0, worldH = wy1 - wy0;
    if (!(worldW > 0) || !(worldH > 0)) return { ok: false, reason: "degenerate-bbox" };

    var z;
    if (opts.zoom != null) {
      if (!isInt(opts.zoom) || opts.zoom < MIN_ZOOM || opts.zoom > maxZoom) return { ok: false, reason: "invalid-zoom" };
      z = opts.zoom;
      var r = tileRange(wx0, wy0, wx1, wy1, z);
      if (r.nx * r.ny > maxTiles) return { ok: false, reason: "too-many-tiles" };
    } else {
      var worldPerPx = Math.max(worldW / vp.width, worldH / vp.height);
      z = zoomForWorldPerPx(worldPerPx, maxZoom);
      while (z > MIN_ZOOM) {                       // lower zoom until the count fits — arithmetic, no enumeration
        var rr = tileRange(wx0, wy0, wx1, wy1, z);
        if (rr.nx * rr.ny <= maxTiles) break;
        z--;
      }
    }

    // Uniform CONTAIN scale (square tiles): fit the whole selected area into BOTH viewport axes, then center
    // it with equal margins so the entire selection is visible (not just some tiles).
    var scale = Math.min(vp.width / worldW, vp.height / worldH);
    var offX = (vp.width - worldW * scale) / 2, offY = (vp.height - worldH * scale) / 2;
    var range = tileRange(wx0, wy0, wx1, wy1, z);
    var tiles = [];
    for (var x = range.x0; x <= range.x1; x++) {
      for (var y = range.y0; y <= range.y1; y++) {
        var t = tileWorldRect(x, y, z);
        tiles.push({ z: z, x: x, y: y, px: {
          left: round2(offX + (t.wx - wx0) * scale), top: round2(offY + (t.wy - wy0) * scale), size: round2(t.size * scale)
        } });
      }
    }
    // The selected area's pixel rectangle (for corner verification and the preview's area outline).
    var area = { left: round2(offX), top: round2(offY), width: round2(worldW * scale), height: round2(worldH * scale) };
    return { ok: true, provider: prov.id, attribution: prov.attribution, zoom: z,
             worldRect: { wx0: wx0, wy0: wy0, wx1: wx1, wy1: wy1 }, area: area, tiles: tiles };
  }
  function round2(v) { return Math.round(v * 100) / 100; }

  // ── cache-only preview viewer ────────────────────────────────────────────────────────────────────
  // Loads planned tiles through opts.load(provider, z, x, y, signal) -> Promise<url|null> — a url ONLY for a
  // successfully loaded image; null = not cached / not usable -> blank square. The concurrency cap is held at
  // the VIEWER level across replacements: an outstanding load occupies a slot until it actually ends, so
  // cancel()+show() can never exceed the cap at once. Superseding a run settles its show() promise
  // immediately (no waiting for the old loader) and ABORTS its outstanding loads via an injected
  // AbortController; a stale result never paints or changes the current run's counters. One attempt per tile
  // — no retries, no downloads. Honest terminal status: empty | partial | complete.
  function createViewer(opts) {
    var load = opts.load, cap = Math.max(1, Math.min(opts.concurrency || 4, 4));
    var onTile = opts.onTile || function () {}, onStatus = opts.onStatus || function () {};
    var makeAbort = opts.abortController ||
      (typeof AbortController !== "undefined" ? function () { return new AbortController(); }
                                              : function () { return { abort: function () {}, signal: undefined }; });
    var run = 0, current = null;
    var active = 0;      // outstanding loader ops across ALL runs — the cap counts these
    var inflight = [];   // their abort controllers

    function emit(ctl, state) { onStatus({ state: state, total: ctl.total, loaded: ctl.loaded, missing: ctl.missing, pending: ctl.total - ctl.loaded - ctl.missing }); }
    function termState(ctl) { return ctl.loaded === 0 ? "empty" : (ctl.loaded < ctl.total ? "partial" : "complete"); }
    function drop(ab) { var k = inflight.indexOf(ab); if (k >= 0) inflight.splice(k, 1); active--; }

    function settle(ctl) {
      if (ctl.done) return;
      if (ctl.my !== run) { ctl.finish(false); return; }                 // superseded -> resolve cancelled
      if (ctl.loaded + ctl.missing === ctl.total) ctl.finish(true, termState(ctl));
    }
    function schedule() {                                                // launch the CURRENT run up to the global cap
      var ctl = current;
      if (!ctl || ctl.done) return;
      while (active < cap && ctl.i < ctl.total) launch(ctl, ctl.tiles[ctl.i++]);
    }
    function launch(ctl, tile) {
      var ab = makeAbort(); inflight.push(ab); active++;
      var myRun = ctl.my;
      Promise.resolve().then(function () { return load(ctl.provider, tile.z, tile.x, tile.y, ab.signal); })
        .then(function (url) {
          drop(ab);
          if (myRun === run && !ctl.done) {
            if (url) { ctl.loaded++; onTile(tile, url); } else { ctl.missing++; }
            if (ctl.loaded + ctl.missing < ctl.total) emit(ctl, "partial");
          }
          settle(ctl); schedule();
        }, function () {
          drop(ab);
          if (myRun === run && !ctl.done) ctl.missing++;                 // failed/aborted load = blank; never retried
          settle(ctl); schedule();
        });
    }
    function supersede() {                                               // settle the prior run + abort its loads
      if (current && !current.done) current.finish(false);
      for (var k = 0; k < inflight.length; k++) { try { inflight[k].abort(); } catch (e) {} }
    }

    function show(plan) {
      run++; supersede();
      var tiles = (plan && plan.ok && Array.isArray(plan.tiles)) ? plan.tiles : [];
      var ctl = { my: run, done: false, loaded: 0, missing: 0, i: 0, tiles: tiles, total: tiles.length, provider: plan && plan.provider };
      current = ctl;
      return new Promise(function (resolve) {
        ctl.finish = function (report, state) {                          // resolve exactly once; report=false => cancelled
          if (ctl.done) return; ctl.done = true;
          if (report) emit(ctl, state);
          resolve({ run: ctl.my, total: ctl.total, loaded: ctl.loaded, missing: ctl.missing, cancelled: !report });
        };
        if (ctl.total === 0) { ctl.finish(true, "empty"); return; }
        emit(ctl, "loading");
        schedule();
      });
    }
    function cancel() { run++; supersede(); }
    return { show: show, cancel: cancel, activeRun: function () { return run; }, outstanding: function () { return active; } };
  }

  return {
    TILE_SIZE: TILE_SIZE, WORLD_PX: WORLD_PX, MIN_ZOOM: MIN_ZOOM, MAX_ZOOM: MAX_ZOOM,
    MERC_LAT_LIMIT: MERC_LAT_LIMIT, DEFAULT_PROVIDER: DEFAULT_PROVIDER, MAX_TILES_PER_VIEW: MAX_TILES_PER_VIEW,
    getProvider: getProvider, providerList: providerList,
    clampZoom: clampZoom, lonLatToWorld: lonLatToWorld, lonLatToTileFrac: lonLatToTileFrac,
    tileXY: tileXY, tileWorldRect: tileWorldRect, zoomForWorldPerPx: zoomForWorldPerPx,
    tileRange: tileRange, validateBbox: validateBbox, planView: planView, createViewer: createViewer
  };
});
