/* Offline maps overview — draws the bundled Natural Earth country/coastline outline as a coarse,
 * offline orientation map. It shows the whole world or an area entered as S,W,N,E; it has no streets,
 * navigation, download, or location. The geometry helpers are exported so they can be unit-tested;
 * create() is the browser controller that loads the outline once, with a deadline and a bounded body,
 * and re-draws it locally without re-fetching. */
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.CCMapOutline = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var WORLD = [-90, -180, 90, 180];   // [S, W, N, E]

  function finiteNum(n) { return typeof n === "number" && isFinite(n); }
  function round2(v) { return Math.round(v * 100) / 100; }

  // Validate an explicit [S,W,N,E] area: finite, ordered (S<N and W<E — a wrapping/antimeridian box is
  // rejected, not guessed), and within Earth bounds. Returns the box or null.
  function validateBbox(b) {
    if (!Array.isArray(b) || b.length !== 4 || !b.every(finiteNum)) return null;
    var s = b[0], w = b[1], n = b[2], e = b[3];
    if (s < -90 || n > 90 || w < -180 || e > 180) return null;
    if (!(s < n) || !(w < e)) return null;
    return [s, w, n, e];
  }

  // Parse a user "S,W,N,E" string into a validated box. Each of the four fields must be present and a
  // plain number BEFORE coercion, so an empty or non-numeric field is rejected rather than read as 0.
  function parseBbox(text) {
    if (typeof text !== "string") return null;
    var parts = text.split(",");
    if (parts.length !== 4) return null;
    var nums = [];
    for (var i = 0; i < 4; i++) {
      var tok = parts[i].trim();
      if (!/^-?\d+(\.\d+)?$/.test(tok)) return null;
      nums.push(Number(tok));
    }
    return validateBbox(nums);
  }

  // Pure projection of a lat/lon into an explicit width x height box under a LINEAR equirectangular fit
  // of bbox=[S,W,N,E], y inverted (north at top). This is the SAME formula the existing Flock overlay
  // uses (reform.js toXY); with width=1000,height=380 the coordinates are identical (a test proves it).
  function project(lat, lon, bbox, width, height) {
    var s = bbox[0], w = bbox[1], n = bbox[2], e = bbox[3];
    var lonSpan = (e - w) || 1e-6, latSpan = (n - s) || 1e-6;
    return [((lon - w) / lonSpan) * width, (1 - (lat - s) / latSpan) * height];
  }

  // Liang-Barsky: clip one segment to the axis-aligned rectangle and return the visible portion as
  // [[ax,ay],[bx,by]] (colinear with the input, so true crossings are preserved), or null if none is
  // visible. This is real segment clipping, not per-vertex clamping.
  function clipSegment(x0, y0, x1, y1, xmin, ymin, xmax, ymax) {
    var dx = x1 - x0, dy = y1 - y0;
    var p = [-dx, dx, -dy, dy];
    var q = [x0 - xmin, xmax - x0, y0 - ymin, ymax - y0];
    var u0 = 0, u1 = 1;
    for (var i = 0; i < 4; i++) {
      if (p[i] === 0) { if (q[i] < 0) return null; }   // parallel to an edge and outside it
      else {
        var t = q[i] / p[i];
        if (p[i] < 0) { if (t > u1) return null; if (t > u0) u0 = t; }
        else { if (t < u0) return null; if (t < u1) u1 = t; }
      }
    }
    return [[x0 + u0 * dx, y0 + u0 * dy], [x0 + u1 * dx, y0 + u1 * dy]];
  }

  // A ring is entirely outside the view bbox (in lat/lon) — a cheap cull before projection/clipping.
  // Malformed points are ignored; a ring with no usable point is treated as outside.
  function ringOutside(ring, bbox) {
    var s = bbox[0], w = bbox[1], n = bbox[2], e = bbox[3];
    var minLon = Infinity, maxLon = -Infinity, minLat = Infinity, maxLat = -Infinity, seen = false;
    for (var i = 0; i < ring.length; i++) {
      var pt = ring[i];
      if (!pt || !finiteNum(pt[0]) || !finiteNum(pt[1])) continue;
      var lon = pt[0], lat = pt[1]; seen = true;
      if (lon < minLon) minLon = lon; if (lon > maxLon) maxLon = lon;
      if (lat < minLat) minLat = lat; if (lat > maxLat) maxLat = lat;
    }
    if (!seen) return true;
    return maxLon < w || minLon > e || maxLat < s || minLat > n;
  }

  // One ring -> an SVG path "d". Each edge is clipped to the [0,width]x[0,height] viewport and only the
  // visible portion is drawn; the path breaks (a new "M") wherever the outline leaves and re-enters, so
  // no artificial edge is drawn along the viewport border. Coordinates are rounded to 2dp to bound size.
  function ringPath(ring, bbox, width, height) {
    var pts = [];
    for (var i = 0; i < ring.length; i++) {
      var pt = ring[i];
      if (!pt || !finiteNum(pt[0]) || !finiteNum(pt[1])) continue;
      var pr = project(pt[1], pt[0], bbox, width, height);
      if (finiteNum(pr[0]) && finiteNum(pr[1])) pts.push(pr);
    }
    var d = "", pen = null;
    for (var j = 0; j + 1 < pts.length; j++) {
      var a = pts[j], b = pts[j + 1];
      var seg = clipSegment(a[0], a[1], b[0], b[1], 0, 0, width, height);
      if (!seg) { pen = null; continue; }
      var ax = round2(seg[0][0]), ay = round2(seg[0][1]), bx = round2(seg[1][0]), by = round2(seg[1][1]);
      if (pen && pen[0] === ax && pen[1] === ay) d += "L" + bx + " " + by;
      else d += "M" + ax + " " + ay + "L" + bx + " " + by;
      pen = [bx, by];
    }
    return d;
  }

  // A GeoJSON FeatureCollection -> [path-d, …] for every Polygon/MultiPolygon ring (exterior + holes),
  // culled to the view bbox and clipped to the viewport. Containers and rings are shape-checked so
  // ordinary incomplete geometry is skipped rather than throwing; this is not a claim of total malformed-
  // input protection. Work is linear in the accepted vertex count.
  function featuresToPaths(geojson, bbox, width, height) {
    var out = [];
    var feats = (geojson && Array.isArray(geojson.features)) ? geojson.features : [];
    for (var f = 0; f < feats.length; f++) {
      var g = feats[f] && feats[f].geometry;
      if (!g || !Array.isArray(g.coordinates)) continue;
      var polys = g.type === "Polygon" ? [g.coordinates] : g.type === "MultiPolygon" ? g.coordinates : [];
      for (var p = 0; p < polys.length; p++) {
        var rings = Array.isArray(polys[p]) ? polys[p] : [];
        for (var r = 0; r < rings.length; r++) {
          var ring = rings[r];
          if (!Array.isArray(ring) || ring.length < 2) continue;
          if (ringOutside(ring, bbox)) continue;
          var d = ringPath(ring, bbox, width, height);
          if (d) out.push(d);
        }
      }
    }
    return out;
  }

  // Structural admission at the load boundary: a FeatureCollection with a non-empty features array.
  // Whether it actually yields drawable geometry is checked separately (world render > 0 paths).
  function isUsableOutline(g) {
    return !!(g && Array.isArray(g.features) && g.features.length > 0);
  }

  function exceedsMax(n, max) { return finiteNum(n) && finiteNum(max) && n > max; }

  // Swallow a cancel/discard promise so it neither goes unhandled nor blocks the caller (fire-and-forget).
  function quiet(p) { if (p && p.then) p.then(null, function () {}); }

  // Load JSON with a bounded body: reject early on an oversized Content-Length, otherwise read the body in
  // chunks and reject once the running total passes maxBytes — never allocating an unbounded body. Streaming
  // is REQUIRED to enforce the bound; if the response exposes no reader we reject recoverably rather than
  // read the whole body via json()/text()/arrayBuffer(). A body we stop reading (non-ok, over-budget, or
  // no-stream) is cancelled and the reader released, so nothing is left dangling; the abort signal is honoured.
  function boundedLoad(fetchImpl, url, headers, maxBytes, signal) {
    return fetchImpl(url, { headers: headers || {}, credentials: "same-origin", signal: signal }).then(function (r) {
      function discardBody() { try { if (r.body && r.body.cancel) quiet(r.body.cancel()); } catch (e) {} }
      if (!r.ok) { discardBody(); return Promise.reject(r.status); }
      var len = r.headers && r.headers.get ? r.headers.get("Content-Length") : null;
      if (len && exceedsMax(Number(len), maxBytes)) { discardBody(); return Promise.reject("too-large"); }
      if (!r.body || !r.body.getReader) { discardBody(); return Promise.reject("no-stream"); }
      var reader = r.body.getReader(), received = 0, chunks = [];
      function release() { try { reader.releaseLock(); } catch (e) {} }
      function cancel() { try { quiet(reader.cancel()); } catch (e) {} }
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) {
            var buf = new Uint8Array(received), off = 0;
            for (var i = 0; i < chunks.length; i++) { buf.set(chunks[i], off); off += chunks[i].length; }
            return JSON.parse(new TextDecoder("utf-8").decode(buf));
          }
          received += res.value.length;
          if (exceedsMax(received, maxBytes)) return Promise.reject("too-large");
          chunks.push(res.value);
          return pump();
        });
      }
      return pump().then(function (value) {
        release();
        return value;
      }, function (error) {
        cancel();
        release();
        return Promise.reject(error);
      });
    });
  }

  // ── browser controller ──────────────────────────────────────────────────────────────────────────
  // Loads the outline ONCE (opts.load(signal) -> Promise<geojson>) behind a deadline, caches the parsed
  // data, and re-projects it for the world view or an area without re-fetching. A timed-out or failed
  // load shows a recoverable Retry; a payload with no usable geometry shows a recoverable "unavailable".
  // A late-arriving stale load can never overwrite a newer attempt. Retry is explicit — there is no
  // polling loop. An area requested during load is remembered and drawn when the load lands.
  function create(opts) {
    var group = opts.group, status = opts.status;
    var W_PX = opts.width || 1000, H_PX = opts.height || 380;
    var load = opts.load, setBusy = opts.setBusy || function () {};
    var DEADLINE = opts.timeoutMs || 15000;
    var timer = opts.setTimeout || (typeof setTimeout !== "undefined" ? setTimeout : null);
    var untimer = opts.clearTimeout || (typeof clearTimeout !== "undefined" ? clearTimeout : null);
    var makeAbort = opts.abortController || (typeof AbortController !== "undefined" ? function () { return new AbortController(); } : function () { return null; });
    var SVGNS = "http://www.w3.org/2000/svg";
    var data = null, loading = false, curBbox = null, lastKey = null;
    var attempt = 0, activeTimer = null, pendingBbox = null;

    function say(state, msg) { if (status) { status.setAttribute("data-state", state); status.textContent = msg || ""; } if (opts.onState) opts.onState(state); }
    function stopTimer() { if (activeTimer != null && untimer) untimer(activeTimer); activeTimer = null; }

    function draw(bbox) {
      var vb = validateBbox(bbox) || WORLD;
      var key = vb.join(",");
      if (data && key === lastKey) { say("ready", ""); return; }   // reuse paths, but clear any prior error/badbbox
      while (group.firstChild) group.removeChild(group.firstChild);
      var paths = featuresToPaths(data, vb, W_PX, H_PX);
      for (var i = 0; i < paths.length; i++) {
        var el = document.createElementNS(SVGNS, "path");
        el.setAttribute("d", paths[i]);
        el.setAttribute("fill", "none");
        el.setAttribute("stroke", opts.stroke || "#3a4553");
        el.setAttribute("stroke-width", "0.8");
        group.appendChild(el);
      }
      curBbox = vb; lastKey = key; say("ready", "");
    }

    function finish(my) { if (my !== attempt) return false; stopTimer(); loading = false; setBusy(false); return true; }

    function open() {
      if (data) { draw(pendingBbox || curBbox || WORLD); pendingBbox = null; return; }
      if (loading) return;
      loading = true; var my = ++attempt;
      var ab = makeAbort();
      say("loading", "Loading offline outline…"); setBusy(true);
      stopTimer();
      if (timer) activeTimer = timer(function () {
        if (my !== attempt) return;
        attempt++;   // invalidate this attempt so a late-arriving load can't overwrite the timeout
        activeTimer = null; loading = false; setBusy(false);
        if (ab) try { ab.abort(); } catch (e) {}
        say("error", "Loading timed out — use Retry.");
      }, DEADLINE);
      Promise.resolve().then(function () { return load(ab ? ab.signal : undefined); }).then(function (g) {
        if (!finish(my)) return;
        if (!isUsableOutline(g) || featuresToPaths(g, WORLD, W_PX, H_PX).length === 0) {
          say("unavailable", "Offline outline is unavailable."); return;
        }
        data = g;
        var pb = pendingBbox; pendingBbox = null;
        draw(pb || WORLD);
      }, function () {
        if (!finish(my)) return;
        say("error", "Could not load the offline outline — use Retry.");
      });
    }

    function area(bbox) {
      var vb = validateBbox(bbox);
      if (!vb) { say("badbbox", "Enter S,W,N,E within -90..90 / -180..180, with S<N and W<E."); return false; }
      if (data) draw(vb); else { pendingBbox = vb; open(); }   // remember the area through the load
      return true;
    }
    function reset() { if (data) draw(WORLD); else { pendingBbox = null; open(); } }
    function retry() { if (!loading) open(); }

    return { open: open, area: area, reset: reset, retry: retry,
             state: function () { return { loaded: !!data, bbox: curBbox && curBbox.slice(), loading: loading }; } };
  }

  return { WORLD: WORLD, validateBbox: validateBbox, parseBbox: parseBbox, project: project,
           clipSegment: clipSegment, featuresToPaths: featuresToPaths, isUsableOutline: isUsableOutline,
           exceedsMax: exceedsMax, boundedLoad: boundedLoad, create: create };
});
