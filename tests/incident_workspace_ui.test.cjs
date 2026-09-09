/* HUNT ▸ Incidents workspace UI — runs the real incident_workspace.js in a stub DOM/window/fetch scope (no
 * jsdom, no browser, no network, no app). Proves: rowsFromReport merges events+unsupported+malformed in
 * physical-line order without collapsing duplicates; pageRows filters by status/category and pages at 100; the
 * import lifecycle (POST octet-stream + CSRF, client-side over-limit reject); preview and download use the exact
 * full response bytes (never a filtered page); the generation guard drops a superseded/aborted response; Clear
 * releases state and revokes the object URL; and leaving the tab aborts an in-flight import.
 */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("fs"), path = require("path");

const SRC = fs.readFileSync(
  path.join(__dirname, "..", "src", "ui", "web", "static", "incident_workspace.js"), "utf8");

function makeEl() {
  return {
    _kids: [], textContent: "", value: "", href: "", download: "", files: null, _clicks: 0,
    appendChild(c) { this._kids.push(c); return c; },
    removeChild(c) { const i = this._kids.indexOf(c); if (i >= 0) this._kids.splice(i, 1); return c; },
    get firstChild() { return this._kids.length ? this._kids[0] : null; },
    addEventListener() {},
    click() { this._clicks++; },
  };
}

const IDS = ["incident-state", "incident-digest", "incident-counts", "incident-allclear-note", "incident-clock",
             "incident-filtered-count", "incident-rows", "incident-preview", "incident-message",
             "incident-filter-status", "incident-filter-category", "incident-file"];

function harness() {
  const ids = {};
  IDS.forEach((id) => { ids[id] = makeEl(); });
  const vis = { on: true };
  const created = [];
  const document = {
    getElementById: (id) => ids[id] || null,
    createElement: () => { const e = makeEl(); created.push(e); return e; },
    querySelector: (sel) => (sel.indexOf('data-sub="incidents"') >= 0 && vis.on ? makeEl() : null),
    addEventListener() {},
  };
  const pending = [], blobs = [], revoked = [];
  let urlSeq = 0;
  const window = {
    CSRF_TOKEN: "tok",
    AbortController: function () { this.aborted = false; this.signal = {}; this.abort = () => { this.aborted = true; }; },
    Blob: function (parts, opts) { this.parts = parts; this.opts = opts; blobs.push(this); },
    URL: {
      createObjectURL: () => "blob:" + (++urlSeq),
      revokeObjectURL: (u) => { revoked.push(u); },
    },
    fetch: (url, opt) => new Promise((resolve, reject) => pending.push({ url, opt, resolve, reject })),
  };
  // eslint-disable-next-line no-new-func
  const api = new Function("window", "document", "fetch",
    SRC + "\nreturn window.CCIncidents;")(window, document, window.fetch);
  const flush = () => new Promise((r) => setImmediate(r));
  const settle = (i, text, ok = true, status = 200) =>
    pending[i].resolve({ ok, status, text: () => Promise.resolve(text) });
  const tableRows = () => ids["incident-rows"]._kids.map((tr) => tr._kids.map((td) => td.textContent));
  return { api, ids, pending, flush, settle, tableRows, created, blobs, revoked, window, vis,
           st: () => ids["incident-state"].textContent, msg: () => ids["incident-message"].textContent };
}

const REPORT = {
  format: "cc-incident-report-v1",
  report_identity: { file_sha256: "abc123", file_bytes: 120 },
  coverage: { total_lines: 5, admitted: 2, blank: 1, unsupported: 1, malformed: 1,
              truncated: false, truncation_reason: null, final_line_without_newline: false },
  clock_uncertainty: { global_chronology: "unsupported" },
  events: [{ line: 1, category: "EVILTWIN", ts: 1000, epoch: 1700000000 },
           { line: 4, category: "DEAUTH_FLOOD", ts: 2000, epoch: 1700000002 }],
  unsupported: [{ line: 3, reason: "unsupported-type" }],
  malformed: [{ line: 5, reason: "invalid-json" }],
};
const REPORT_TEXT = JSON.stringify(REPORT);

// ── pure presentation ────────────────────────────────────────────────────────────────────────────

test("rowsFromReport merges all rows in physical-line order and keeps duplicates distinct", () => {
  const api = harness().api;
  const rows = api.rowsFromReport(REPORT);
  assert.deepEqual(rows.map((r) => r.line), [1, 3, 4, 5]);
  assert.deepEqual(rows.map((r) => r.status), ["admitted", "unsupported", "admitted", "malformed"]);
  // duplicate incident content at different physical lines is NOT collapsed
  const dup = api.rowsFromReport({ events: [{ line: 1, category: "EVILTWIN", ts: 5, epoch: 9 },
                                            { line: 2, category: "EVILTWIN", ts: 5, epoch: 9 }],
                                   unsupported: [], malformed: [] });
  assert.equal(dup.length, 2);
  assert.deepEqual(dup.map((r) => r.line), [1, 2]);
});

test("pageRows filters by status and category and pages at 100 with a filtered count", () => {
  const api = harness().api;
  const rows = api.rowsFromReport(REPORT);
  assert.deepEqual(api.pageRows(rows, { status: "admitted" }).rows.map((r) => r.line), [1, 4]);
  assert.deepEqual(api.pageRows(rows, { category: "EVILTWIN" }).rows.map((r) => r.line), [1]);
  // 250 admitted rows -> 3 pages of <=100, filteredCount preserved
  const many = { events: Array.from({ length: 250 }, (_, i) => ({ line: i + 1, category: "PROBE_FLOOD",
                  ts: 0, epoch: 0 })), unsupported: [], malformed: [] };
  const all = api.rowsFromReport(many);
  const p0 = api.pageRows(all, { status: "admitted", page: 0 });
  assert.equal(p0.rows.length, 100); assert.equal(p0.filteredCount, 250); assert.equal(p0.pages, 3);
  assert.equal(api.pageRows(all, { status: "admitted", page: 2 }).rows.length, 50);
  assert.equal(api.pageRows(all, { status: "admitted", page: 99 }).page, 2);   // clamped
});

// ── import lifecycle ─────────────────────────────────────────────────────────────────────────────

test("import: POSTs raw octet-stream with CSRF, then paints coverage + table on success", async () => {
  const H = harness();
  H.api.importFile({ size: 40 });
  assert.equal(H.pending.length, 1);
  const opt = H.pending[0].opt;
  assert.equal(opt.method, "POST");
  assert.equal(opt.headers["Content-Type"], "application/octet-stream");
  assert.equal(opt.headers["X-CSRF-Token"], "tok");
  assert.ok(opt.signal, "an AbortController signal is attached");
  assert.equal(H.st(), "loading");
  H.settle(0, REPORT_TEXT); await H.flush();
  assert.equal(H.st(), "ready");
  assert.deepEqual(H.tableRows().map((r) => r[0]), ["1", "3", "4", "5"]);
  assert.match(H.ids["incident-counts"].textContent, /admitted 2/);
});

test("import: a file over 2 MiB is rejected client-side without any fetch", () => {
  const H = harness();
  H.api.importFile({ size: 2 * 1024 * 1024 + 1 });
  assert.equal(H.pending.length, 0, "no request is sent");
  assert.equal(H.st(), "failed");
  assert.match(H.msg(), /2 MiB/);
});

test("import: zero admitted rows is explicitly not an all-clear", async () => {
  const H = harness();
  const empty = { ...REPORT, coverage: { ...REPORT.coverage, admitted: 0, total_lines: 3 },
                  events: [], unsupported: REPORT.unsupported, malformed: REPORT.malformed };
  H.api.importFile({ size: 10 }); H.settle(0, JSON.stringify(empty)); await H.flush();
  assert.match(H.ids["incident-allclear-note"].textContent, /not an all-clear/i);
});

test("import: a rejected upload shows the fixed error, no report", async () => {
  const H = harness();
  H.api.importFile({ size: 10 });
  H.settle(0, JSON.stringify({ error: "incomplete-body" }), false, 400); await H.flush();
  assert.equal(H.st(), "failed");
  assert.match(H.msg(), /ended early/);
});

// ── preview / download use the exact full bytes ────────────────────────────────────────────────────

test("preview and download use the exact full response bytes, not a filtered page", async () => {
  const H = harness();
  H.api.importFile({ size: 10 }); H.settle(0, REPORT_TEXT); await H.flush();
  H.api.setFilter("malformed", "all");             // filter the table down...
  assert.deepEqual(H.tableRows().map((r) => r[0]), ["5"]);
  H.api.preview();
  assert.equal(H.ids["incident-preview"].textContent, REPORT_TEXT);   // ...preview is still the FULL projection
  H.api.download();
  assert.equal(H.blobs.length, 1);
  assert.deepEqual(H.blobs[0].parts, [REPORT_TEXT]);                   // download bytes == full projection
  const a = H.created[H.created.length - 1];
  assert.equal(a.download, "cc-incident-report.json");
  assert.equal(a._clicks, 1);
});

test("a second download revokes the prior object URL", async () => {
  const H = harness();
  H.api.importFile({ size: 10 }); H.settle(0, REPORT_TEXT); await H.flush();
  H.api.download(); H.api.download();
  assert.equal(H.revoked.length, 1, "the first URL is revoked before the second is created");
});

// ── generation guard + Clear + navigation ───────────────────────────────────────────────────────────

test("generation guard: a superseded (older) import response does not paint", async () => {
  const H = harness();
  H.api.importFile({ size: 10 });                  // pending[0]
  H.api.importFile({ size: 10 });                  // replace -> aborts [0], pending[1]
  assert.equal(H.pending[0].opt.signal && true, true);
  H.settle(1, JSON.stringify({ ...REPORT, report_identity: { file_sha256: "NEW", file_bytes: 1 } }));
  await H.flush();
  H.settle(0, JSON.stringify({ ...REPORT, report_identity: { file_sha256: "OLD", file_bytes: 1 } }));
  await H.flush();
  assert.match(H.ids["incident-digest"].textContent, /NEW/);
  assert.ok(!/OLD/.test(H.ids["incident-digest"].textContent), "the stale response must not paint");
});

test("Clear releases the report, revokes the object URL, and bumps the generation", async () => {
  const H = harness();
  H.api.importFile({ size: 10 }); H.settle(0, REPORT_TEXT); await H.flush();
  H.api.download();                                // creates an object URL
  H.api.clear();
  assert.equal(H.st(), "empty");
  assert.equal(H.ids["incident-rows"]._kids.length, 0);
  assert.equal(H.revoked.length, 1);
  // a response from before Clear can no longer paint
  const H2 = harness();
  H2.api.importFile({ size: 10 }); H2.api.clear();
  H2.settle(0, REPORT_TEXT); await H2.flush();
  assert.equal(H2.st(), "empty");
});

test("leaving the tab (nav hook) aborts an in-flight import and drops its late response", async () => {
  const H = harness();
  H.vis.on = true; H.api.syncVisibility();         // enter (no fetch — import is user-driven)
  assert.equal(H.pending.length, 0);
  H.api.importFile({ size: 10 });                  // pending[0]
  H.vis.on = false; H.api.syncVisibility();        // leave -> abort + generation bump
  H.settle(0, REPORT_TEXT); await H.flush();
  assert.notEqual(H.st(), "ready", "a late response must not paint after leaving the tab");
});

// ── report replacement / failure clears stale presentation (root UI-1) ─────────────────────────────

test("replacing report A with B clears A's preview/rows/digest before B loads and on success", async () => {
  const H = harness();
  H.api.importFile({ size: 10 }); H.settle(0, REPORT_TEXT); await H.flush();
  H.api.preview();
  assert.equal(H.ids["incident-preview"].textContent, REPORT_TEXT);   // A is shown
  H.api.importFile({ size: 10 });                                     // start replacement B
  assert.equal(H.ids["incident-preview"].textContent, "");           // A's preview cleared at replacement
  assert.equal(H.ids["incident-rows"]._kids.length, 0);
  assert.equal(H.ids["incident-digest"].textContent, "");
  assert.equal(H.st(), "loading");
  const B = JSON.stringify({ ...REPORT, report_identity: { file_sha256: "BBB", file_bytes: 7 } });
  H.settle(1, B); await H.flush();
  assert.match(H.ids["incident-digest"].textContent, /BBB/);         // B is shown
  assert.equal(H.ids["incident-preview"].textContent, "");           // preview blank until re-requested (not A)
});

test("a failed replacement clears the old presentation, not just the model", async () => {
  const H = harness();
  H.api.importFile({ size: 10 }); H.settle(0, REPORT_TEXT); await H.flush();
  H.api.preview();
  H.api.importFile({ size: 10 });                                    // replacement B
  H.settle(1, JSON.stringify({ error: "incomplete-body" }), false, 400); await H.flush();
  assert.equal(H.st(), "failed");
  assert.equal(H.ids["incident-preview"].textContent, "");          // no stale A preview
  assert.equal(H.ids["incident-rows"]._kids.length, 0);
  assert.equal(H.ids["incident-digest"].textContent, "");
  assert.match(H.msg(), /ended early/);
});

test("an over-limit replacement clears the old presentation", async () => {
  const H = harness();
  H.api.importFile({ size: 10 }); H.settle(0, REPORT_TEXT); await H.flush();
  H.api.preview();
  H.api.importFile({ size: 2 * 1024 * 1024 + 1 });
  assert.equal(H.st(), "failed");
  assert.equal(H.ids["incident-preview"].textContent, "");
  assert.equal(H.ids["incident-rows"]._kids.length, 0);
});

// ── Clear resets the visible controls, not just internal state (root UI-2) ──────────────────────────

test("Clear resets the status/category selects, the file input, and the message", async () => {
  const H = harness();
  H.ids["incident-filter-status"].value = "unsupported";
  H.ids["incident-filter-category"].value = "EVILTWIN";
  H.ids["incident-file"].value = "fakepath";
  H.api.importFile({ size: 10 });
  H.settle(0, JSON.stringify({ error: "invalid-body" }), false, 400); await H.flush();
  assert.notEqual(H.msg(), "");                                      // a failure message is present
  H.api.clear();
  assert.equal(H.ids["incident-filter-status"].value, "all");
  assert.equal(H.ids["incident-filter-category"].value, "all");
  assert.equal(H.ids["incident-file"].value, "");                    // chooser reset so the same file re-fires
  assert.equal(H.msg(), "");                                         // stale message cleared
  assert.equal(H.st(), "empty");
});

// ── canceled navigation settles the loading state (root UI-3) ───────────────────────────────────────

test("leaving during a load settles to empty (not stranded loading); reentry stays settled", async () => {
  const H = harness();
  H.vis.on = true; H.api.syncVisibility();
  H.api.importFile({ size: 10 });                                    // loading, pending[0]
  assert.equal(H.st(), "loading");
  H.vis.on = false; H.api.syncVisibility();                          // leave -> abort + settle
  assert.equal(H.st(), "empty");                                     // not stranded on loading
  H.settle(0, REPORT_TEXT); await H.flush();                         // late response suppressed
  assert.equal(H.st(), "empty");
  H.vis.on = true; H.api.syncVisibility();                           // reentry: no auto-fetch, still settled
  assert.equal(H.pending.length, 1);
  assert.equal(H.st(), "empty");
});

test("leaving with a completed report retains it across navigation (reentry preserved)", async () => {
  const H = harness();
  H.vis.on = true; H.api.syncVisibility();
  H.api.importFile({ size: 10 }); H.settle(0, REPORT_TEXT); await H.flush();
  assert.equal(H.st(), "ready");
  H.vis.on = false; H.api.syncVisibility();                          // leave with a completed report
  assert.match(H.ids["incident-digest"].textContent, /abc123/);     // retained, not blanked
  H.vis.on = true; H.api.syncVisibility();                           // reentry
  assert.equal(H.st(), "ready");
  assert.match(H.ids["incident-digest"].textContent, /abc123/);
});
