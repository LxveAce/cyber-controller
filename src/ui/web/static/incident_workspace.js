/* HUNT ▸ Incidents workspace — a local imported-report viewer.
 *
 * Lets the operator choose a local AntiHunter incidents.jsonl file and POST its raw bytes to
 * /api/incidents/import (same-origin, auth + CSRF). The server returns the accepted default-redacted projection;
 * this module keeps that exact response TEXT in memory and renders it: coverage counts + source/clock
 * uncertainty from the full report, a physical-line-order table (status/category filter, <=100 rows per page),
 * and a full-report Preview (rendered as text) + Download of the SAME bytes under a fixed name. It sends nothing
 * until a file is chosen, retains no identifiers, and holds nothing in localStorage/session/cache.
 *
 * Report presentation is released through one path (discardReport) so a replacement or a failure never leaves a
 * previous report's preview/rows/digest on screen, Clear also resets the visible controls (selects, file input,
 * message), and a canceled navigation settles to the retained completed report or an empty state rather than a
 * stranded "loading". A generation counter guards overlapping imports, Clear, and main-nav/subtab navigation so
 * only the newest request may paint; leaving the tab aborts an in-flight import. Preview and Download always use
 * the full projection, never a filtered page.
 */
(function () {
  "use strict";

  var MAX_BYTES = 2 * 1024 * 1024;              // client-side pre-check; the server enforces its own 2 MiB cap
  var PAGE_SIZE = 100;
  var CATEGORIES = ["EVILTWIN", "BEACON_FORGE", "DEAUTH_FORGE", "DEAUTH_FLOOD", "PROBE_FLOOD", "CSA_SPOOF",
                    "SSID_CONFUSION"];
  var STATUSES = ["admitted", "unsupported", "malformed"];
  var DOWNLOAD_NAME = "cc-incident-report.json";

  var generation = 0;          // bumped on every import / Clear / hide; a response paints only if still current
  var inflight = null;         // the live AbortController, or null
  var current = null;          // { text, report } for a completed import, or null
  var objectUrl = null;        // the outstanding download object URL, or null
  var filterStatus = "all", filterCategory = "all", pageIndex = 0;
  var wasVisible = false;

  function el(id) { return document.getElementById(id); }
  function win() { return (typeof window !== "undefined") ? window : {}; }

  function incidentsVisible() {
    return !!(document.querySelector && document.querySelector('.view.on .sub.on[data-sub="incidents"]'));
  }

  // ── pure presentation ───────────────────────────────────────────────────────────────────────────────
  // Merge admitted events with unsupported/malformed rows into one physical-line-ordered list, WITHOUT
  // collapsing duplicates (each row keeps its own line = distinct identity).
  function rowsFromReport(report) {
    var rows = (report.events || []).map(function (e) {
      return { line: e.line, status: "admitted", category: e.category, ts: e.ts, epoch: e.epoch, reason: "" };
    });
    ["unsupported", "malformed"].forEach(function (st) {
      (report[st] || []).forEach(function (it) {
        rows.push({ line: it.line, status: st, category: "", ts: null, epoch: null, reason: it.reason });
      });
    });
    return rows.sort(function (a, b) { return a.line - b.line; });
  }

  function pageRows(rows, opts) {
    opts = opts || {};
    var st = STATUSES.indexOf(opts.status) >= 0 ? opts.status : "all";
    var cat = CATEGORIES.indexOf(opts.category) >= 0 ? opts.category : "all";
    var selected = rows.filter(function (r) {
      return (st === "all" || r.status === st) && (cat === "all" || r.category === cat);
    });
    var pages = Math.max(1, Math.ceil(selected.length / PAGE_SIZE));
    var idx = Number.isInteger(opts.page) ? Math.min(Math.max(opts.page, 0), pages - 1) : 0;
    return { rows: selected.slice(idx * PAGE_SIZE, (idx + 1) * PAGE_SIZE),
             filteredCount: selected.length, page: idx, pages: pages };
  }

  // ── rendering ───────────────────────────────────────────────────────────────────────────────────────
  function setState(word) { var s = el("incident-state"); if (s) s.textContent = word; }
  function setText(id, text) { var e = el(id); if (e) e.textContent = text; }

  function renderCoverage(report) {
    var c = report.coverage || {};
    var id = report.report_identity || {};
    setText("incident-digest", (id.file_sha256 || "unknown") + "  (" + (id.file_bytes || 0) + " bytes)");
    setText("incident-counts",
      "processed " + (c.total_lines || 0) + " lines — admitted " + (c.admitted || 0) + ", blank " +
      (c.blank || 0) + ", unsupported " + (c.unsupported || 0) + ", malformed " + (c.malformed || 0) +
      (c.truncated ? " — TRUNCATED: " + (c.truncation_reason || "") : ""));
    // zero admitted is never an all-clear; say so explicitly.
    setText("incident-allclear-note",
      (c.admitted ? "" : "No admitted events — this is not an all-clear; review malformed/unsupported/coverage."));
    var u = report.clock_uncertainty || {};
    setText("incident-clock", "Clock: global ordering " + (u.global_chronology || "unsupported") +
      "; claimed ts/epoch are not verified times.");
  }

  function renderTable(report) {
    var body = el("incident-rows");
    if (!body) return;
    while (body.firstChild) body.removeChild(body.firstChild);
    var all = rowsFromReport(report);
    var pg = pageRows(all, { status: filterStatus, category: filterCategory, page: pageIndex });
    pageIndex = pg.page;
    pg.rows.forEach(function (r) {
      var tr = document.createElement("tr");
      [String(r.line), r.status + (r.category ? " · " + r.category : ""),
       (r.ts == null ? "" : String(r.ts)), (r.epoch == null ? "" : String(r.epoch)), r.reason].forEach(function (v) {
        var td = document.createElement("td");
        td.textContent = v;               // render as text; never innerHTML
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    // coverage totals stay tied to the FULL report; this count is the filtered view only.
    setText("incident-filtered-count",
      pg.filteredCount + " rows match filter (page " + (pg.page + 1) + " of " + pg.pages + ")");
  }

  function render() {
    if (!current) { renderEmptyView(); return; }
    renderCoverage(current.report);
    renderTable(current.report);
    var total = (current.report.coverage || {}).total_lines || 0;
    setState(total === 0 ? "empty" : "ready");
  }

  // Clear ALL report presentation (digest/counts/note/clock/filtered-count/message/rows/preview) to a blank
  // state. Used on Clear, and via discardReport before any replacement or failure so no stale view survives.
  function renderEmptyView() {
    setState("empty");
    ["incident-digest", "incident-counts", "incident-allclear-note", "incident-clock",
     "incident-filtered-count", "incident-message"].forEach(function (id) { setText(id, ""); });
    var body = el("incident-rows");
    if (body) while (body.firstChild) body.removeChild(body.firstChild);
    var preview = el("incident-preview");
    if (preview) preview.textContent = "";
  }

  // The single report-release path: drop the in-memory report, revoke any download URL, and blank the view.
  function discardReport() {
    current = null;
    revokeUrl();
    renderEmptyView();
  }

  function renderMessage(word, message) { setState(word); setText("incident-message", message || ""); }

  var ERRORS = {
    "unsupported-content-type": "That file could not be sent as raw bytes.",
    "length-required": "The upload had no valid length.",
    "payload-too-large": "That report is larger than the 2 MiB limit.",
    "incomplete-body": "The upload ended early; nothing was parsed.",
    "invalid-body": "The upload could not be read.",
  };

  // ── import lifecycle ──────────────────────────────────────────────────────────────────────────────
  function invalidateInflight() {
    if (inflight) { try { inflight.abort(); } catch (e) { /* already settled */ } inflight = null; }
    generation++;              // any pending .then sees a stale generation and will not paint
  }

  function importFile(file) {
    invalidateInflight();      // replacing a file drops any prior in-flight import
    var mine = generation;
    if (!file) return;
    discardReport();           // clear the previous report's presentation before starting the replacement
    if (typeof file.size === "number" && file.size > MAX_BYTES) {
      renderMessage("failed", ERRORS["payload-too-large"]);   // over-limit: fixed message, no fetch, no stale view
      return;
    }
    setState("loading");
    var Ctrl = win().AbortController;
    var ctrl = Ctrl ? new Ctrl() : null;
    inflight = ctrl;
    return win().fetch("/api/incidents/import", {
      method: "POST", credentials: "same-origin", signal: ctrl ? ctrl.signal : undefined,
      headers: { "X-CSRF-Token": win().CSRF_TOKEN || "", "Content-Type": "application/octet-stream" },
      body: file,
    }).then(function (r) {
      return r.text().then(function (t) { return { ok: r.ok, status: r.status, text: t }; });
    }).then(function (res) {
      if (mine !== generation) return;      // superseded by a newer import / Clear / tab-leave
      inflight = null;
      applyResponse(res);
    }, function () {
      if (mine !== generation) return;      // aborted or failed after being superseded
      inflight = null;
      discardReport();                      // a network failure clears any prior view, not just the model
      renderMessage("failed", "The import could not be completed.");
    });
  }

  function applyResponse(res) {
    discardReport();                        // release the prior report/view before showing this result
    if (!res.ok) {
      var code = "";
      try { code = (JSON.parse(res.text) || {}).error || ""; } catch (e) { code = ""; }
      renderMessage("failed", ERRORS[code] || "The import was rejected.");
      return;
    }
    var report;
    try { report = JSON.parse(res.text); } catch (e) { renderMessage("failed", "Malformed report."); return; }
    current = { text: res.text, report: report };   // keep the EXACT bytes for preview + download
    pageIndex = 0;
    render();                               // paints coverage + table; preview stays blank until re-requested
  }

  // ── preview / download: always the full projection (never a filtered page) ──────────────────────────
  function preview() {
    if (!current) return;
    var p = el("incident-preview");
    if (p) p.textContent = current.text;            // full report bytes, rendered as text
  }

  function revokeUrl() {
    if (objectUrl && win().URL && win().URL.revokeObjectURL) {
      try { win().URL.revokeObjectURL(objectUrl); } catch (e) { /* ignore */ }
    }
    objectUrl = null;
  }

  function download() {
    if (!current) return;
    revokeUrl();                                     // release any prior URL first
    var blob = new (win().Blob)([current.text], { type: "application/json" });
    objectUrl = win().URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = objectUrl;
    a.download = DOWNLOAD_NAME;
    if (a.click) a.click();                          // URL is revoked on the next download or on Clear
  }

  // Clear resets the report AND the visible controls: the status/category selects, the file input value (so the
  // same file can be re-chosen and still fire a change event) and the status message — not just internal state.
  function clear() {
    invalidateInflight();
    discardReport();
    filterStatus = "all"; filterCategory = "all"; pageIndex = 0;
    var sf = el("incident-filter-status"), cf = el("incident-filter-category"), chooser = el("incident-file");
    if (sf) sf.value = "all";
    if (cf) cf.value = "all";
    if (chooser) chooser.value = "";
  }

  function setFilter(status, category) {
    filterStatus = STATUSES.indexOf(status) >= 0 ? status : "all";
    filterCategory = CATEGORIES.indexOf(category) >= 0 ? category : "all";
    pageIndex = 0;
    if (current) renderTable(current.report);
  }

  function setPage(index) {
    if (!Number.isInteger(index)) return;
    pageIndex = index;
    if (current) renderTable(current.report);
  }

  /* Called by reform.js after any navigation (main-view OR subtab). Leaving the tab aborts an in-flight import
   * and bumps the generation so a late response cannot paint, then settles the view: a completed report stays
   * shown (reentry works), otherwise the pane returns to an empty state rather than a stranded "loading".
   * Nothing is fetched on becoming visible (import is user-driven). */
  function syncVisibility() {
    var vis = incidentsVisible();
    if (vis && !wasVisible) { wasVisible = true; }
    else if (!vis && wasVisible) { wasVisible = false; invalidateInflight(); render(); }
  }

  window.CCIncidents = {
    rowsFromReport: rowsFromReport, pageRows: pageRows, importFile: importFile, applyResponse: applyResponse,
    preview: preview, download: download, clear: clear, setFilter: setFilter, setPage: setPage,
    syncVisibility: syncVisibility,
  };

  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("DOMContentLoaded", function () {
      var chooser = el("incident-file");
      if (chooser) chooser.addEventListener("change", function () {
        var f = chooser.files && chooser.files[0];
        if (f) importFile(f);
      });
      var wire = function (id, fn) { var b = el(id); if (b) b.addEventListener("click", fn); };
      wire("incident-clear", clear);
      wire("incident-preview-btn", preview);
      wire("incident-download-btn", download);
      wire("incident-prev", function () { setPage(pageIndex - 1); });
      wire("incident-next", function () { setPage(pageIndex + 1); });
      var sf = el("incident-filter-status"), cf = el("incident-filter-category");
      var onFilter = function () { setFilter(sf ? sf.value : "all", cf ? cf.value : "all"); };
      if (sf) sf.addEventListener("change", onFilter);
      if (cf) cf.addEventListener("change", onFilter);
    });
  }
})();
