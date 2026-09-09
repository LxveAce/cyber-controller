/* Read-only Meshtastic status UI — runs the real mesh_status.js in a stub DOM/window/fetch scope: viewModel
 * state mapping (availability-driven, leak-free), the loading/ready/unavailable/failed paints, and the
 * generation guard for overlapping refreshes AND main-nav/subtab visibility changes (an obsolete or
 * left-view response must not paint while hidden; returning requests a fresh snapshot). No browser/network/app.
 */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("fs"), path = require("path");

const SRC = fs.readFileSync(path.join(__dirname, "..", "src", "ui", "web", "static", "mesh_status.js"), "utf8");

function makeEl() {
  return {
    _kids: [], textContent: "", className: "",
    appendChild(c) { this._kids.push(c); return c; },
    removeChild(c) { const i = this._kids.indexOf(c); if (i >= 0) this._kids.splice(i, 1); return c; },
    get firstChild() { return this._kids.length ? this._kids[0] : null; },
    addEventListener() {},
  };
}

function harness() {
  const ids = { "mesh-status-body": makeEl(), "mesh-status-state": makeEl(), "mesh-status-refresh": makeEl() };
  const state = { visible: true };
  const document = {
    getElementById: (id) => ids[id] || null,
    createElement: () => makeEl(),
    // Mesh is "visible" only when state.visible; the module queries for the on-view's on-sub mesh element.
    querySelector: (sel) => (sel.indexOf('data-sub="mesh"') >= 0 && state.visible ? makeEl() : null),
    addEventListener() {},
  };
  const pending = [];
  function fetchStub() { return new Promise((resolve, reject) => pending.push({ resolve, reject })); }
  const window = { CSRF_TOKEN: "t" };
  // eslint-disable-next-line no-new-func
  const api = new Function("window", "document", "fetch", SRC + "\nreturn window.CCMeshStatus;")(window, document, fetchStub);
  const flush = () => new Promise((r) => setImmediate(r));
  const bodyLines = () => ids["mesh-status-body"]._kids.map((k) => k.textContent);
  const settle = (i, body, ok = true) => pending[i][ok ? "resolve" : "reject"]({ ok, json: () => Promise.resolve(body) });
  const setVisible = (v) => { state.visible = v; };
  return { api, ids, pending, flush, bodyLines, settle, setVisible,
           st: () => ids["mesh-status-state"].textContent };
}

const READY = {
  available: true, identity: { num: 7, name: "Owner Node · direct" },
  battery: { state: "percent", percent: 84, voltage: 4.02 }, link: { state: "known", snr_db: 6.5 },
  freshness: { last_heard_epoch: 999970, last_heard_age_s: 30, telemetry_age: "unknown",
               read_at_epoch: 1000000, read_at_is: "request-time", clock: "ok" },
  readiness: { config_complete: true, publication_admitted: true, transport: "bound" },
};

// ── viewModel: availability-driven, reworded, leak-free ──────────────────────────────────────────

test("viewModel: unavailable uses observation-availability wording (no physical-disconnect claim)", () => {
  const vm = harness().api.viewModel({ available: false, reason: "provider-absent" });
  assert.equal(vm.state, "unavailable");
  assert.match(vm.message, /observation reader is available/);
  assert.ok(!/connected|disconnect/i.test(vm.message));
});

test("viewModel: each unavailable reason maps to a message; none imply physical connectivity", () => {
  const api = harness().api;
  for (const reason of ["provider-error", "owner-not-admitted", "inventory-not-ready",
                        "transport-uncertain", "transport-retired"]) {
    const vm = api.viewModel({ available: false, reason });
    assert.equal(vm.state, "unavailable");
    assert.ok(!/physically|disconnected node/i.test(vm.message));
  }
});

test("viewModel: ready renders literal battery/link/freshness; no stale-flag state", () => {
  const vm = harness().api.viewModel(READY);
  assert.equal(vm.state, "ready");
  assert.equal(vm.battery, "Battery: 84% (4.02 V)");
  assert.equal(vm.link, "Link SNR: 6.5 dB");
  assert.equal(vm.freshness, "Last heard: 30 s ago");
});

test("viewModel: external / unknown battery, unknown link, uncertain-clock freshness", () => {
  const api = harness().api;
  assert.equal(api.viewModel({ ...READY, battery: { state: "external", percent: null, voltage: 4.9 } }).battery,
    "Power: external (4.9 V)");
  assert.equal(api.viewModel({ ...READY, battery: { state: "unknown", percent: null, voltage: null } }).battery,
    "Battery: unknown");
  assert.equal(api.viewModel({ ...READY, link: { state: "unknown", snr_db: null } }).link, "Link SNR: unknown");
  assert.equal(api.viewModel({ ...READY,
    freshness: { ...READY.freshness, last_heard_age_s: "unknown", clock: "ambiguous" } }).freshness,
    "Last heard: time uncertain (node clock)");
});

test("viewModel carries no GPS/neighbor/channel/config keys or values", () => {
  const vm = harness().api.viewModel(READY);
  const tokens = JSON.stringify(vm).toLowerCase();
  for (const f of ["lat", "lon", "latitude", "longitude", "gps", "position", "neighbor", "channel", "psk", "lora"])
    assert.ok(!tokens.includes(f), `leaked ${f}`);
});

// ── paints ─────────────────────────────────────────────────────────────────────────────────────

test("refresh: loading -> ready paints owner lines + explicit unknown telemetry age", async () => {
  const H = harness();
  H.api.refresh();
  assert.equal(H.st(), "loading");
  H.settle(0, READY); await H.flush();
  assert.equal(H.st(), "ready");
  const lines = H.bodyLines();
  assert.ok(lines.some((l) => l.includes("Owner Node · direct") && l.includes("node 7")));
  assert.ok(lines.some((l) => /measurement age: unknown/i.test(l)));
});

test("refresh: unavailable + failed states", async () => {
  const H = harness();
  H.api.refresh(); H.settle(0, { available: false, reason: "inventory-not-ready" }); await H.flush();
  assert.equal(H.st(), "unavailable");
  assert.ok(H.bodyLines().some((l) => /inventory is not ready/.test(l)));
  const H2 = harness();
  H2.api.refresh(); H2.settle(0, null, false); await H2.flush();
  assert.equal(H2.st(), "failed");
});

// ── generation guard: overlap + main-nav/subtab leave ─────────────────────────────────────────────

test("generation guard: a stale earlier response does not overwrite a newer refresh", async () => {
  const H = harness();
  H.api.refresh(); H.api.refresh();
  H.settle(1, { ...READY, identity: { num: 7, name: "NEWER" } }); await H.flush();
  H.settle(0, { ...READY, identity: { num: 7, name: "OLDER" } }); await H.flush();
  assert.ok(H.bodyLines().some((l) => l.includes("NEWER")));
  assert.ok(!H.bodyLines().some((l) => l.includes("OLDER")));
});

test("MS-4: leaving via MAIN navigation drops an in-flight response (no hidden stale paint)", async () => {
  const H = harness();               // starts visible; DOMContentLoaded not fired, so drive explicitly
  H.setVisible(true); H.api.syncVisibility();   // enter -> refresh (pending[0])
  assert.equal(H.pending.length, 1);
  H.setVisible(false); H.api.syncVisibility();  // main-nav away -> generation bumped, view hidden
  H.settle(0, { ...READY, identity: { num: 7, name: "LATE_AFTER_MAIN_LEAVE" } }); await H.flush();
  assert.ok(!H.bodyLines().some((l) => l.includes("LATE_AFTER_MAIN_LEAVE")), "must not paint while hidden");
});

test("MS-4: returning to a shown Mesh view requests a fresh snapshot", async () => {
  const H = harness();
  H.setVisible(true); H.api.syncVisibility();    // enter -> pending[0]
  H.setVisible(false); H.api.syncVisibility();   // leave
  H.setVisible(true); H.api.syncVisibility();    // return -> a NEW request
  assert.equal(H.pending.length, 2, "returning issued a fresh read");
});

test("syncVisibility: no refresh while it stays hidden or stays visible", () => {
  const H = harness();
  H.setVisible(false); H.api.syncVisibility(); H.api.syncVisibility();
  assert.equal(H.pending.length, 0);
  H.setVisible(true); H.api.syncVisibility(); H.api.syncVisibility();   // second call: still visible, no extra
  assert.equal(H.pending.length, 1);
});
