/* Offline Data view: paste SigMF .sigmf-meta text, Analyze -> same-origin summarize, render text-only.
 *
 * Boundaries: no file read/picker, no IQ/sample data, no external fetch, no map,
 * no device discovery, no persistence/upload, and NO request on opening the view. The only request sends the
 * explicitly pasted bytes to CC's own backend after Analyze. Output is text-only (textContent); filenames,
 * URLs and location values are shown inert (never linked/opened). Clear, editing the input, or leaving the
 * view invalidates any in-flight request so a late response cannot repopulate discarded data.
 *
 * Self-initializes on load (loaded before reform.js). Exposes window.CCOfflineData for testing; the rail
 * controller needs no hook -- staleness is enforced here via a generation token + AbortController + an
 * active-view check.
 */
(function () {
  "use strict";

  var ENDPOINT = "/api/offline-metadata/summarize";
  var MAX_BYTES = 262144; // matches the parser + route cap; enforced client-side BEFORE any request (server also enforces)
  var MAX_VALUE_CHARS = 2000; // bound for a single rendered metadata value (structured values -> bounded JSON text)

  // The ONLY status/error pairs the backend documents (API contract da117f25) as DEFINITE pre-summary
  // rejections, established before the summarizer runs. A status alone, an unknown error code, or a known
  // error on a different status does NOT establish this contract, so those outcomes are reported as
  // unconfirmed rather than as a proven rejection.
  var KNOWN_REJECTIONS = {
    "400": ["incomplete-body", "invalid-body"],
    "411": ["length-required"],
    "413": ["payload-too-large"],
    "415": ["unsupported-content-type"],
  };
  function isEstablishedRejection(status, error) {
    if (!error) return false;
    var allowed = KNOWN_REJECTIONS[String(status)];
    return !!allowed && allowed.indexOf(error) !== -1;
  }

  // Byte length of a string as UTF-8 (what the server will receive). Primary path is TextEncoder; when it is
  // absent or throws, count the bytes directly -- surrogate pairs are one 4-byte code point, and an unpaired
  // surrogate encodes as the U+FFFD replacement character (3 bytes), matching TextEncoder exactly. Both paths
  // are correct UTF-8 byte counts, so the client-side cap holds with or without TextEncoder.
  function utf8Bytes(s) {
    if (typeof TextEncoder !== "undefined") {
      try { return new TextEncoder().encode(s).length; } catch (e) {}
    }
    var bytes = 0;
    for (var i = 0; i < s.length; i++) {
      var c = s.charCodeAt(i);
      if (c < 0x80) {
        bytes += 1;
      } else if (c < 0x800) {
        bytes += 2;
      } else if (c >= 0xD800 && c <= 0xDBFF) {            // high surrogate
        var lo = i + 1 < s.length ? s.charCodeAt(i + 1) : 0;
        if (lo >= 0xDC00 && lo <= 0xDFFF) { bytes += 4; i++; }  // valid pair -> one supplementary code point
        else { bytes += 3; }                                    // unpaired high surrogate -> U+FFFD
      } else if (c >= 0xDC00 && c <= 0xDFFF) {            // unpaired low surrogate -> U+FFFD
        bytes += 3;
      } else {                                            // rest of the BMP
        bytes += 3;
      }
    }
    return bytes;
  }

  // Render any metadata value as bounded TEXT with structure + values intact. Structured values (objects/
  // arrays) become bounded JSON text -- presentation only; links, locations and extensions are NOT
  // interpreted. Large integers already arrive as exact decimal strings from the backend (lossless contract).
  function toText(v) {
    if (v === null || v === undefined) return String(v);
    if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") return String(v);
    var s;
    try { s = JSON.stringify(v); } catch (e) { s = String(v); }
    if (typeof s !== "string") s = String(v);
    if (s.length > MAX_VALUE_CHARS) s = s.slice(0, MAX_VALUE_CHARS) + "…[truncated]";
    return s;
  }

  function create(doc, root) {
    var input = root.querySelector("#od-input");
    var analyzeBtn = root.querySelector("#od-analyze");
    var clearBtn = root.querySelector("#od-clear");
    var statusEl = root.querySelector("#od-status");
    var detailEl = root.querySelector("#od-detail");
    if (!input || !analyzeBtn || !clearBtn || !statusEl || !detailEl) return null;

    var gen = 0;              // monotonic; a response whose token != gen is stale and dropped
    var controller = null;    // current AbortController

    function setState(state, statusText) {
      root.setAttribute("data-state", state);
      statusEl.textContent = statusText;
    }

    function invalidateInFlight() {
      gen++;
      if (controller) { try { controller.abort(); } catch (e) {} controller = null; }
    }

    function viewActive() {
      // The rail controller marks the active view with class "on"; drop a response if we've navigated away.
      return root.classList ? root.classList.contains("on") : true;
    }

    function csrfToken() {
      if (typeof window !== "undefined" && window.CSRF_TOKEN) return window.CSRF_TOKEN;
      var m = doc.querySelector('meta[name="csrf-token"]');
      return m ? m.getAttribute("content") : "";
    }

    // --- text-only rendering of the parser result (no innerHTML; filenames/URLs/location are inert text) ---
    function line(parts) { return parts.join(""); }

    function renderDiagnostics(out, result) {
      var diags = result.diagnostics || [];
      for (var i = 0; i < diags.length; i++) {
        out.push(line(["  - ", String(diags[i].field), " [", String(diags[i].code), "]: ", String(diags[i].detail)]));
      }
      if (result.diagnostics_truncated) out.push("  - (diagnostics truncated)");
    }

    function renderEntries(out, label, block) {
      out.push(line([label, ": count=", String(block.count), block.truncated ? " (entries truncated)" : ""]));
      var entries = block.entries || [];
      for (var i = 0; i < entries.length; i++) {
        var e = entries[i];
        var rec = e.recognized || {};
        var recKeys = Object.keys(rec);
        for (var k = 0; k < recKeys.length; k++) {
          out.push(line(["    ", recKeys[k], " = ", toText(rec[recKeys[k]]), "  (recognized)"]));
        }
        var un = e.uninterpreted || {};
        var unKeys = Object.keys(un);
        for (var u = 0; u < unKeys.length; u++) {
          out.push(line(["    ", unKeys[u], " = ", toText(un[unKeys[u]].value),
                         "  (uninterpreted / ", String(un[unKeys[u]].semantics), ")"]));
        }
      }
    }

    function renderSummary(result) {
      var s = result.summary || {};
      var out = [];
      out.push(line(["Format: ", String(s.format), " v", String(s.version)]));
      if (s.conformance_note) out.push(String(s.conformance_note));
      var dt = s.datatype || {};
      out.push(line(["Datatype: ", String(dt.raw), " -> ", dt.complex ? "complex " : "real ", String(dt.element_format),
                     ", ", String(dt.bytes_per_channel_sample), " bytes/channel-sample, byte order ", String(dt.byte_order)]));
      out.push(line(["Channels: ", s.num_channels === null || s.num_channels === undefined ? "unknown (not supplied)" : String(s.num_channels)]));
      var rg = s.recognized_global || {};
      var rgKeys = Object.keys(rg);
      if (rgKeys.length) {
        out.push("Recognized global:");
        for (var i = 0; i < rgKeys.length; i++) out.push(line(["  ", rgKeys[i], " = ", toText(rg[rgKeys[i]])]));
      }
      var ug = s.uninterpreted_global || {};
      var ugKeys = Object.keys(ug);
      if (ugKeys.length) {
        out.push("Uninterpreted global (values preserved, not interpreted):");
        for (var j = 0; j < ugKeys.length; j++) {
          out.push(line(["  ", ugKeys[j], " = ", toText(ug[ugKeys[j]].value), "  (", String(ug[ugKeys[j]].semantics), ")"]));
        }
      }
      if (s.captures) renderEntries(out, "Captures", s.captures);
      if (s.annotations) renderEntries(out, "Annotations", s.annotations);
      var ext = s.extensions_opaque || [];
      if (ext.length) {
        out.push("Extensions (opaque, uninterpreted):");
        for (var x = 0; x < ext.length; x++) out.push(line(["  ", String(ext[x].name), " v", String(ext[x].version)]));
      }
      var inert = s.inert_references || {};
      var inKeys = Object.keys(inert);
      if (inKeys.length) {
        out.push("Inert references (shown as text only, never opened or fetched):");
        for (var y = 0; y < inKeys.length; y++) out.push(line(["  ", inKeys[y], " = ", toText(inert[inKeys[y]])]));
      }
      return out.join("\n");
    }

    function render(result) {
      var status = result.status;
      if (status === "summarized") {
        setState("summarized", "Summarized. Recognized descriptors were checked (not a full-conformance verdict).");
        detailEl.textContent = renderSummary(result);
      } else if (status === "unsupported") {
        var o = ["Unsupported for this subset (accepted version: SigMF 1.2.6)."];
        renderDiagnostics(o, result);
        setState("unsupported", "Unsupported.");
        detailEl.textContent = o.join("\n");
      } else if (status === "invalid") {
        var oi = ["Invalid metadata:"];
        renderDiagnostics(oi, result);
        setState("invalid", "Invalid.");
        detailEl.textContent = oi.join("\n");
      } else {
        setState("request-failure", "Unexpected response.");
        detailEl.textContent = "The backend returned an unrecognized result shape.";
      }
    }

    function analyze() {
      invalidateInFlight();
      var myGen = gen;
      var text = input.value != null ? input.value : "";
      if (text.length === 0) { setState("empty", "Paste SigMF metadata, then Analyze."); detailEl.textContent = ""; return; }
      var nbytes = utf8Bytes(text);
      if (nbytes > MAX_BYTES) {   // enforce the UTF-8 byte cap BEFORE any request; no fetch is sent
        setState("request-failure", "Input is " + nbytes + " bytes; exceeds the " + MAX_BYTES + "-byte limit. Not sent.");
        detailEl.textContent = "";
        return;
      }
      controller = (typeof AbortController !== "undefined") ? new AbortController() : null;
      setState("busy", "Analyzing…");
      detailEl.textContent = "";
      fetch(ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream", "X-CSRF-Token": csrfToken() },
        body: text,
        cache: "no-store",
        signal: controller ? controller.signal : undefined,
      }).then(function (res) {
        return res.text().then(function (t) { return { ok: res.ok, status: res.status, text: t }; });
      }).then(function (r) {
        if (myGen !== gen || !viewActive()) return;   // stale: discarded/replaced/navigated-away
        controller = null;
        var parsed = null;
        try { parsed = JSON.parse(r.text); } catch (e) { parsed = null; }
        if (!r.ok) {
          var code = parsed && parsed.error ? parsed.error : ("http-" + r.status);
          if (isEstablishedRejection(r.status, parsed && parsed.error)) {
            // A documented status/error pair: the API guarantees rejection before the summarizer runs.
            setState("request-failure", "Request failed (" + code + ").");
            detailEl.textContent = "The request was rejected before analysis. No metadata was interpreted.";
          } else {
            // A status alone, an unknown error code, or a known code on an unexpected status does not
            // establish rejection-before-analysis; keep the outcome uncertain rather than overclaim it.
            setState("request-failure", "Request outcome unconfirmed (" + code + ").");
            detailEl.textContent = "The backend reported an error, but the processing outcome could not be confirmed. No summary was shown.";
          }
          return;
        }
        if (!parsed) { setState("request-failure", "Malformed backend response."); detailEl.textContent = ""; return; }
        render(parsed);
      }).catch(function (err) {
        if (myGen !== gen || !viewActive()) return;   // aborted or navigated away: not a user-facing failure
        if (err && err.name === "AbortError") return;
        controller = null;
        // The fetch promise (or reading its response) rejected. This can be a network failure OR a
        // received-but-unreadable response, so do not assert the backend was never reached or that no
        // bytes arrived; report the outcome as unconfirmed.
        setState("request-failure", "Request outcome unconfirmed.");
        detailEl.textContent = "The request could not be completed, so its outcome could not be confirmed. The backend may not have been reached, or a response may not have been readable.";
      });
    }

    function clear() {
      invalidateInFlight();
      input.value = "";
      setState("empty", "Cleared. Paste SigMF metadata, then Analyze.");
      detailEl.textContent = "";
    }

    analyzeBtn.addEventListener("click", analyze);
    clearBtn.addEventListener("click", clear);
    // Any edit invalidates an in-flight request AND settles out of any non-empty state: the page never
    // sticks at "busy", and a completed result is never left labeled current for changed input.
    input.addEventListener("input", function () {
      invalidateInFlight();
      if (root.getAttribute("data-state") !== "empty") {
        setState("empty", "Input changed — Analyze to summarize.");
        detailEl.textContent = "";
      }
    });

    // Called by the rail on nav enter/leave (mirrors CCMeshStatus/CCIncidents.syncVisibility). On LEAVE it
    // aborts + invalidates any in-flight request and settles out of "busy", so a leave-then-return can never
    // let the previous request paint. On enter it does nothing (NO request on open/enter).
    function syncVisibility() {
      if (viewActive()) return;
      var wasBusy = root.getAttribute("data-state") === "busy";
      invalidateInFlight();
      if (wasBusy) { setState("empty", "Analysis canceled (left the view). Analyze to retry."); detailEl.textContent = ""; }
    }

    setState("empty", "Paste SigMF metadata, then Analyze.");   // initial state; NO request on open
    return { analyze: analyze, clear: clear, render: render, renderSummary: renderSummary,
             syncVisibility: syncVisibility, _invalidate: invalidateInFlight, _gen: function () { return gen; } };
  }

  function init() {
    if (typeof document === "undefined") return;
    var root = document.getElementById("view-offline-data");
    if (!root) return;
    window.CCOfflineData = create(document, root);
  }

  // Export the factory for isolated testing with a fake DOM; auto-init in a real browser.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { create: create };
  }
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
    else init();
  }
})();
