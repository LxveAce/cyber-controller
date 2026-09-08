/* Cache-only browser loader for offline raster tiles.
 *
 * Fetches ONE already-cached tile from the fixed same-origin endpoint
 * `GET /api/map-tiles/<provider>/<z>/<x>/<y>.png` and returns a decode-verified result. It never
 * reaches an external host, never retries, and never downloads: online tiles remain the Qt tab's
 * opt-in path. It is the piece the future Cached-tiles view calls per visible tile.
 *
 * Settled-result contract (loadTile resolves to exactly one of these; it never rejects):
 *   { status: "ok", url, mime }   a real PNG/JPEG that DECODED; `url` is a data: URL (CSP img-src
 *                                 allows 'self' and data:, NOT blob:). Count a tile only on "ok".
 *   { status: "missing" }         a genuine 204 cache miss -> draw a blank square.
 *   { status: "http-error", code } any other non-200 (400/404/502/...) or a transport failure (code 0).
 *   { status: "too-large" }       a body / chunk that would exceed the byte budget (rejected without
 *                                 being retained).
 *   { status: "read-error" }      the body stream errored / interrupted mid-read.
 *   { status: "decode-error" }    bytes present but not a decodable PNG/JPEG (magic bytes alone are
 *                                 NOT enough -- the image must actually decode).
 *   { status: "timeout" }         the deadline (headers + body + decode) elapsed.
 *   { status: "aborted" }         a caller abort signal fired.
 *   { status: "invalid" }         provider/coordinate failed validation -- no request was made.
 *
 * Lifecycle: readBoundedResp owns the body reader and CANCELS + RELEASES it on EVERY terminal path
 * (EOF, over-budget, interrupted read, abort via signal) so `response.body.locked` is false
 * afterward and no rejected cancel promise is left unhandled; it never rejects (it resolves a status).
 * loadTile wraps it behind a single finalizer that clears the deadline timer, removes the caller's
 * abort listener, cancels the image decode, and (via the shared abort signal) makes readBoundedResp
 * release the reader; an interrupted read settles promptly (not at the deadline); a response arriving
 * after the call settled has its body cancelled.
 *
 * Byte-budget guarantee (honest): `maxBytes` bounds the RETAINED ENCODED image bytes (the accumulated
 * chunks), NOT total browser memory -- decoding also stores the base64 data: URL (~4/3 the bytes) plus
 * a concatenation buffer and the decoded bitmap. A stream chooses its own chunk sizes, so one transient
 * chunk may itself exceed the budget; the loader rejects such a chunk as "too-large" WITHOUT
 * accumulating it (peak retained encoded bytes stay <= maxBytes). Caller overrides for maxBytes and the
 * deadline are clamped to the module ceilings (1 MiB, DEFAULT_DEADLINE_MS). */
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.CCMapRasterLoader = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var MIN_ZOOM = 0, MAX_ZOOM = 19;
  var MAX_TILE_BYTES = 1024 * 1024;          // 1 MiB per-tile ceiling (matches the backend route)
  var DEFAULT_DEADLINE_MS = 15000;           // covers headers + body + decode; also the override ceiling
  // Mirrors src/core/map_tiles.py PROVIDERS keys; the server is the authority (unknown -> its 404),
  // but the loader validates first so a bad name never even builds a request URL.
  var KNOWN_PROVIDERS = ["carto-dark", "carto-light", "carto-voyager", "osm", "osm-de"];

  function isInt(n) { return typeof n === "number" && isFinite(n) && Math.floor(n) === n; }
  function quiet(p) { if (p && p.then) p.then(null, function () {}); }   // swallow a fire-and-forget promise

  function tileUrl(provider, z, x, y) {
    return "/api/map-tiles/" + provider + "/" + z + "/" + x + "/" + y + ".png";
  }

  // The supported image MIME implied by the magic bytes, or null. Content, never a filename suffix.
  function magicMime(bytes) {
    if (bytes.length >= 8 && bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e &&
        bytes[3] === 0x47 && bytes[4] === 0x0d && bytes[5] === 0x0a && bytes[6] === 0x1a &&
        bytes[7] === 0x0a) return "image/png";
    if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return "image/jpeg";
    return null;
  }

  function bytesToDataUrl(bytes, mime) {
    var b64;
    if (typeof btoa !== "undefined") {
      var bin = "";
      for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
      b64 = btoa(bin);
    } else {
      b64 = Buffer.from(bytes).toString("base64");   // node fallback (tests)
    }
    return "data:" + mime + ";base64," + b64;
  }

  // Bounded body read that OWNS its reader's cleanup. Resolves (never rejects) one of:
  //   { status:"ok", bytes } | { status:"too-large" } | { status:"read-error" } | { status:"aborted" }
  //   | { status:"decode-error" } (no stream reader). On every terminal path it cancels + releases the
  // reader (or cancels the body if no reader was taken), so response.body.locked is false afterward and
  // no rejected cancel promise is left unhandled. An optional `signal` cancels an in-flight read.
  function readBoundedResp(resp, maxBytes, signal) {
    var len = resp.headers && resp.headers.get ? resp.headers.get("Content-Length") : null;
    if (len && isFinite(Number(len)) && Number(len) > maxBytes) {
      if (resp.body && resp.body.cancel) { try { quiet(resp.body.cancel()); } catch (e) {} }
      return Promise.resolve({ status: "too-large" });
    }
    if (!resp.body || !resp.body.getReader) return Promise.resolve({ status: "decode-error" });
    var reader = resp.body.getReader(), received = 0, chunks = [], done = false, onAbort = null;
    function release() { try { if (reader.releaseLock) reader.releaseLock(); } catch (e) {} }
    function cancelRelease() { try { quiet(reader.cancel()); } catch (e) {} release(); }
    return new Promise(function (resolve) {
      function fin(res, cleanup) {
        if (done) return; done = true;
        if (onAbort && signal && signal.removeEventListener) { try { signal.removeEventListener("abort", onAbort); } catch (e) {} }
        cleanup();
        resolve(res);
      }
      if (signal) {
        if (signal.aborted) { fin({ status: "aborted" }, cancelRelease); return; }
        onAbort = function () { fin({ status: "aborted" }, cancelRelease); };
        if (signal.addEventListener) signal.addEventListener("abort", onAbort);
      }
      function pump() {
        reader.read().then(function (r) {
          if (done) return;
          if (r.done) {
            var buf = new Uint8Array(received), off = 0;
            for (var i = 0; i < chunks.length; i++) { buf.set(chunks[i], off); off += chunks[i].length; }
            fin({ status: "ok", bytes: buf }, release);      // drained -> just release the lock
            return;
          }
          if (received + r.value.length > maxBytes) { fin({ status: "too-large" }, cancelRelease); return; }
          received += r.value.length; chunks.push(r.value);
          pump();
        }, function () { fin({ status: "read-error" }, cancelRelease); });   // interrupted -> cancel + release
      }
      pump();
    });
  }

  // Default decode: load the data: URL into an Image and resolve when it actually decodes. Returns
  // { promise, cancel } so the loader can release the image on abort/timeout. Tests inject a fake.
  function defaultDecode(dataUrl) {
    var cancelled = false, img = (typeof Image !== "undefined") ? new Image() : null;
    var promise = new Promise(function (resolve, reject) {
      if (!img) { reject(new Error("no Image")); return; }
      img.onload = function () { if (!cancelled) resolve(img); };
      img.onerror = function () { if (!cancelled) reject(new Error("decode")); };
      img.src = dataUrl;
      if (img.decode) img.decode().then(function () { if (!cancelled) resolve(img); }, function () {});
    });
    return { promise: promise, cancel: function () { cancelled = true; if (img) { img.onload = null; img.onerror = null; img.src = ""; } } };
  }

  // Clamp a caller override to a positive value no greater than a ceiling, else the ceiling default.
  function clampCeil(v, ceil) { return (typeof v === "number" && isFinite(v) && v > 0) ? Math.min(v, ceil) : ceil; }

  function createLoader(opts) {
    opts = opts || {};
    var fetchImpl = opts.fetch || (typeof fetch !== "undefined" ? fetch : null);
    var decode = opts.decode || defaultDecode;
    var deadlineMs = clampCeil(opts.deadlineMs, DEFAULT_DEADLINE_MS);
    var maxBytes = Math.floor(clampCeil(opts.maxBytes, MAX_TILE_BYTES));
    var providers = opts.providers || KNOWN_PROVIDERS;
    var maxZoom = isInt(opts.maxZoom) ? Math.max(MIN_ZOOM, Math.min(opts.maxZoom, MAX_ZOOM)) : MAX_ZOOM;
    var makeAbort = opts.abortController ||
      (typeof AbortController !== "undefined" ? function () { return new AbortController(); } : function () { return null; });
    var timer = opts.setTimeout || (typeof setTimeout !== "undefined" ? setTimeout : null);
    var untimer = opts.clearTimeout || (typeof clearTimeout !== "undefined" ? clearTimeout : null);

    function validCoord(provider, z, x, y) {
      if (providers.indexOf(provider) < 0) return false;
      if (!isInt(z) || z < MIN_ZOOM || z > maxZoom) return false;
      var n = Math.pow(2, z);
      return isInt(x) && isInt(y) && x >= 0 && x < n && y >= 0 && y < n;
    }

    function loadTile(provider, z, x, y, signal) {
      if (!validCoord(provider, z, x, y)) return Promise.resolve({ status: "invalid" });
      var ab = makeAbort();
      return new Promise(function (resolve) {
        var settled = false, t = null, decodeCtl = null, onAbort = null;
        function finalize(res) {
          if (settled) return; settled = true;
          if (t != null && untimer) untimer(t);
          if (onAbort && signal && signal.removeEventListener) { try { signal.removeEventListener("abort", onAbort); } catch (e) {} }
          if (decodeCtl && decodeCtl.cancel) { try { decodeCtl.cancel(); } catch (e) {} }
          resolve(res);
        }
        function abortFetch() { if (ab) { try { ab.abort(); } catch (e) {} } }        // -> readBoundedResp releases the reader
        function cancelBody(resp) { if (resp && resp.body && resp.body.cancel) { try { quiet(resp.body.cancel()); } catch (e) {} } }

        if (signal) {
          if (signal.aborted) { abortFetch(); finalize({ status: "aborted" }); return; }
          onAbort = function () { abortFetch(); finalize({ status: "aborted" }); };
          if (signal.addEventListener) signal.addEventListener("abort", onAbort);
        }
        if (timer) t = timer(function () { abortFetch(); finalize({ status: "timeout" }); }, deadlineMs);

        Promise.resolve().then(function () {
          return fetchImpl(tileUrl(provider, z, x, y), { credentials: "same-origin", cache: "no-store", signal: ab ? ab.signal : undefined });
        }).then(function (resp) {
          if (settled) { cancelBody(resp); return; }                 // late response after abort/timeout
          if (resp.status === 204) { finalize({ status: "missing" }); return; }
          if (!resp.ok) { cancelBody(resp); finalize({ status: "http-error", code: resp.status }); return; }
          return readBoundedResp(resp, maxBytes, ab ? ab.signal : undefined).then(function (rb) {
            if (settled) return;
            if (rb.status !== "ok") { finalize({ status: rb.status }); return; }   // too-large / read-error / aborted / decode-error
            var mime = magicMime(rb.bytes);
            if (!mime) { finalize({ status: "decode-error" }); return; }
            var url = bytesToDataUrl(rb.bytes, mime);
            try {
              decodeCtl = decode(url);
              decodeCtl.promise.then(function () { finalize({ status: "ok", url: url, mime: mime }); },
                                     function () { finalize({ status: "decode-error" }); });
            } catch (e) {
              finalize({ status: "decode-error" });   // a synchronous decoder throw must not hang or go unhandled
            }
          });
        }, function () {
          finalize({ status: (ab && ab.signal && ab.signal.aborted) ? "aborted" : "http-error", code: 0 });
        });
      });
    }

    // Adapter for a viewer whose per-tile loader is load(provider,z,x,y,signal) -> Promise<url|null>
    // (null = blank): only a decoded "ok" yields a url; every other status is a blank square. The richer
    // status set above is what the eventual UI uses to explain failures rather than claim uncached.
    function viewerLoad(provider, z, x, y, signal) {
      return loadTile(provider, z, x, y, signal).then(function (r) { return r.status === "ok" ? r.url : null; });
    }

    return { loadTile: loadTile, viewerLoad: viewerLoad, validCoord: validCoord };
  }

  return {
    MIN_ZOOM: MIN_ZOOM, MAX_ZOOM: MAX_ZOOM, MAX_TILE_BYTES: MAX_TILE_BYTES,
    DEFAULT_DEADLINE_MS: DEFAULT_DEADLINE_MS, KNOWN_PROVIDERS: KNOWN_PROVIDERS,
    isInt: isInt, tileUrl: tileUrl, magicMime: magicMime, bytesToDataUrl: bytesToDataUrl,
    readBoundedResp: readBoundedResp, createLoader: createLoader
  };
});
