/* Inert fixture proof for the passive BLE session-history reader (oldest-first, forward cursor).
 * No DOM, socket, timer or fetch: the page transport, timers and clock are all injected. */
const test = require("node:test"), assert = require("node:assert/strict");
const history = require("../src/ui/web/static/ble_history.js");

const flush = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };

function row(seq, extra) {
  return Object.assign({
    schema_version: 1, run_id: "r1", seq: seq, observed_at: "2026-09-07T08:00:00.000000+00:00",
    kind: "ble_found", source: { port: "COM1", firmware: "fw", connection_id: "1" },
    address: "aa:bb:cc:dd:ee:01", address_type: "public", addressable: true,
    label: "dev" + seq, rssi: -50, report_meta: {}, meta: {},
  }, extra || {});
}
function page(rows, opts) {
  opts = opts || {};
  const mode = opts.mode || "memory";
  return { httpStatus: opts.httpStatus || 200, body: {
    status: { requested_mode: mode, effective_mode: mode, storage: mode === "memory" ? "memory" : "none",
      available: mode === "memory", reason: opts.reason || null, durable: false,
      policy_lifetime: "restart", run_id: opts.run_id || "r1", counters: {} },
    rows: rows, cursor: "cursor" in opts ? opts.cursor : "c",
    has_more: !!opts.has_more, earliest_seq: "earliest_seq" in opts ? opts.earliest_seq : 1 } };
}

function fixture(overrides) {
  const pageCalls = [], states = [], deliveries = [], timers = new Map();
  let id = 0, clock = 0;
  const options = Object.assign({
    fetchPage: (cursor, signal) => new Promise((resolve, reject) => pageCalls.push({ cursor, signal, resolve, reject })),
    onPage: (rows, meta) => deliveries.push({ rows, meta }),
    onStatus: (state, extra) => states.push({ state, extra }),
    setTimeout: (fn, ms) => { timers.set(++id, { fn, ms }); return id; },
    clearTimeout: key => timers.delete(key), now: () => clock, domCap: 500,
  }, overrides || {});
  const control = history.create(options);
  return { control, pageCalls, states, deliveries, timers,
    lastState: () => states.length ? states[states.length - 1].state : null,
    lastRows: () => deliveries.length ? deliveries[deliveries.length - 1].rows.map(r => r.seq) : null,
    lastMeta: () => deliveries.length ? deliveries[deliveries.length - 1].meta : null,
    expire: () => { clock = 15000; for (const t of [...timers.values()]) t.fn(); } };
}

test("refresh loads the oldest page with no cursor and reports fresh", async () => {
  const f = fixture();
  const p = f.control.refresh(); await flush();
  assert.equal(f.pageCalls[0].cursor, null, "oldest page uses no cursor");
  f.pageCalls[0].resolve(page([row(1), row(2)], { cursor: "c2", has_more: true }));
  assert.equal(await p, true);
  assert.deepEqual(f.lastRows(), [1, 2]);
  assert.equal(f.lastMeta().has_more, true);
  assert.equal(f.lastState(), "fresh");
});

test("load more appends the next (newer) page and forwards the held cursor", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(1), row(2)], { cursor: "c2", has_more: true })); await flush();
  const m = f.control.loadMore(); await flush();
  assert.equal(f.pageCalls[1].cursor, "c2", "load more forwards the held cursor, never reverses it");
  f.pageCalls[1].resolve(page([row(3), row(4)], { cursor: "c3", has_more: false }));
  assert.equal(await m, true);
  assert.deepEqual(f.lastRows(), [1, 2, 3, 4], "newer rows append after older");
  assert.equal(f.lastMeta().has_more, false);
});

test("caught up: load more is a no-op when has_more is false", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(1)], { cursor: "c1", has_more: false })); await flush();
  assert.equal(await f.control.loadMore(), false);
  assert.equal(f.pageCalls.length, 1, "no second fetch when caught up");
});

test("disabled: a 200 disabled page clears rows and reports disabled", async () => {
  const f = fixture();
  const p = f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([], { mode: "disabled", cursor: null, has_more: false, earliest_seq: null, reason: "disabled" }));
  await p;
  assert.equal(f.lastState(), "disabled");
  assert.deepEqual(f.lastRows(), []);
});

test("unavailable: a 503 reports unavailable with the finite reason", async () => {
  const f = fixture();
  const p = f.control.refresh(); await flush();
  f.pageCalls[0].resolve({ httpStatus: 503, body: { status: { reason: "start_failed" } } });
  await p;
  assert.equal(f.lastState(), "unavailable");
  assert.equal(f.states[f.states.length - 1].extra.reason, "start_failed");
});

test("bad request: a 400 is a finite error with no recovery and no auto-retry", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(1)], { cursor: "c1", has_more: true })); await flush();
  const m = f.control.loadMore(); await flush();
  f.pageCalls[1].resolve({ httpStatus: 400, body: { reason: "cursor_malformed" } });
  await m;
  assert.equal(f.lastState(), "error");
  assert.equal(f.pageCalls.length, 2, "a 400 never triggers an automatic re-fetch");
});

test("expired 410 triggers exactly one automatic recovery from the oldest page", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(1), row(2)], { cursor: "c2", has_more: true })); await flush();
  const m = f.control.loadMore(); await flush();
  f.pageCalls[1].resolve({ httpStatus: 410, body: { reason: "cursor_expired", earliest_seq: 5 } });
  await m; await flush();
  assert.ok(f.states.some(s => s.state === "expired"), "an expired state is surfaced");
  assert.equal(f.pageCalls[2].cursor, null, "the single recovery re-fetches from the oldest page");
  f.pageCalls[2].resolve(page([row(5), row(6)], { cursor: "c6", has_more: false })); await flush();
  assert.deepEqual(f.lastRows(), [5, 6], "recovery restarts from the oldest still-available page");
});

test("a second expiry during recovery does not loop (one recovery per user action)", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(1)], { cursor: "c1", has_more: true })); await flush();
  const m = f.control.loadMore(); await flush();
  f.pageCalls[1].resolve({ httpStatus: 410, body: { reason: "cursor_expired", earliest_seq: 5 } });
  await m; await flush();
  f.pageCalls[2].resolve({ httpStatus: 410, body: { reason: "cursor_expired", earliest_seq: 9 } });
  await flush();
  assert.equal(f.pageCalls.length, 3, "the recovery is bounded to one; no runaway re-fetch loop");
  assert.equal(f.lastState(), "stale", "when the one recovery also expires, end in the terminal " +
    "manual-refresh state, not 'expired' (which implies an in-flight reload)");
  assert.deepEqual(f.lastRows(), [], "the stale view is cleared, not left showing obsolete rows");
});

test("a run_id change on load more restarts from the oldest page", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(1), row(2)], { cursor: "c2", has_more: true, run_id: "r1" })); await flush();
  const m = f.control.loadMore(); await flush();
  f.pageCalls[1].resolve(page([row(3, { run_id: "r2" })], { cursor: "c3", has_more: true, run_id: "r2" }));
  await m; await flush();
  assert.ok(f.states.some(s => s.state === "expired"), "a restart is reported as expired");
  assert.equal(f.pageCalls[2].cursor, null, "and recovers from the oldest page of the new run");
});

test("the DOM cap trims the oldest loaded rows with a trimmed notice", async () => {
  const f = fixture({ domCap: 3 });
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(1), row(2)], { cursor: "c2", has_more: true })); await flush();
  assert.equal(f.lastMeta().trimmed, false);
  const m = f.control.loadMore(); await flush();
  f.pageCalls[1].resolve(page([row(3), row(4)], { cursor: "c4", has_more: false }));
  await m;
  assert.deepEqual(f.lastRows(), [2, 3, 4], "oldest dropped to keep the cap");
  assert.equal(f.lastMeta().trimmed, true);
});

test("unauthorized: a 401 reports unauthorized and loads nothing", async () => {
  const f = fixture();
  const p = f.control.refresh(); await flush();
  const err = new Error("unauthorized"); err.status = 401;
  f.pageCalls[0].reject(err);
  await p;
  assert.equal(f.lastState(), "unauthorized");
});

test("a malformed page body is a finite error, not a crash", async () => {
  const f = fixture();
  const p = f.control.refresh(); await flush();
  f.pageCalls[0].resolve({ httpStatus: 200, body: { status: { effective_mode: "memory" }, rows: "nope", has_more: false, cursor: null, earliest_seq: null } });
  await p;
  assert.equal(f.lastState(), "error");
});

test("suspend cancels the in-flight read and stops delivery", async () => {
  const f = fixture();
  const p = f.control.refresh(); await flush();
  f.control.suspend();
  assert.equal(await p, false);
  f.pageCalls[0].resolve(page([row(1)], { has_more: false }));
  await flush();
  assert.equal(f.deliveries.length, 0, "a suspended reader delivers nothing");
});

test("a forced refresh supersedes an in-flight read without double-delivering", async () => {
  const f = fixture();
  const first = f.control.refresh(); await flush();
  const second = f.control.refresh(); await flush();
  assert.equal(await first, false, "the superseded read resolves false");
  assert.ok(f.pageCalls[0].signal.aborted, "and its request is aborted");
  f.pageCalls[1].resolve(page([row(1)], { has_more: false }));
  assert.equal(await second, true);
  assert.deepEqual(f.lastRows(), [1]);
});

// ── lifecycle regressions: bfcache restore, run-change invalidation, sticky trim ─────────────

test("wake after suspend restores actions without reloading (bfcache persisted pageshow)", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(1), row(2)], { cursor: "c2", has_more: true })); await flush();
  f.control.suspend();                 // pagehide
  f.control.wake();                    // persisted pageshow (card open OR closed) — no reload
  assert.equal(f.pageCalls.length, 1, "wake issues no request and does not reload");
  assert.deepEqual(f.lastRows(), [1, 2], "loaded rows preserved (re-presented), not re-fetched");
  assert.equal(f.lastState(), "fresh", "settled to a finite state, not left on loading");
  const r = f.control.refresh(); await flush();   // a later user action is no longer blocked
  assert.equal(f.pageCalls.length, 2, "refresh after wake issues a request (not permanently suspended)");
  f.pageCalls[1].resolve(page([row(1)], { has_more: false }));
  assert.equal(await r, true);
});

test("wake settles a cancelled first-load from loading to the neutral idle state", async () => {
  const f = fixture();
  f.control.refresh(); await flush();            // first load in flight
  assert.equal(f.lastState(), "loading");
  f.control.suspend();                           // pagehide cancels it (nothing loaded yet)
  f.control.wake();                              // persisted pageshow, card open
  assert.equal(f.lastState(), "idle", "no rows -> neutral idle, never stuck on loading/aria-busy");
  assert.deepEqual(f.lastRows(), []);
  const r = f.control.refresh(); await flush();  // manual recovery still works
  assert.equal(f.pageCalls.length, 2);
  f.pageCalls[1].resolve(page([row(1)], { has_more: false }));
  assert.equal(await r, true);
});

// ── default transport resource retirement (inert fetch/stream doubles) ────────

function fakeStream(script) {
  const calls = { cancel: 0, releaseLock: 0 };
  let i = 0;
  const reader = {
    read() {
      const step = script.steps[i++];
      if (!step || step.done) return Promise.resolve({ done: true });
      if (step.throwRead) return Promise.reject(new Error("read failed (double)"));
      return Promise.resolve({ done: false, value: step.value });
    },
    cancel() { calls.cancel++; return script.cancelRejects ? Promise.reject(new Error("cancel rejected")) : Promise.resolve(); },
    releaseLock() { calls.releaseLock++; },
  };
  return { calls, reader };
}

async function runTransport(script, status) {
  const s = fakeStream(script);
  const savedFetch = global.fetch, hadWindow = "window" in global, savedWindow = global.window;
  global.window = { CSRF_TOKEN: "" };
  global.fetch = () => Promise.resolve({
    status: status || 200,
    body: { getReader: () => s.reader, cancel() { s.calls.cancel++; } },
  });
  let error = null, result = null;
  try { result = await history.transport(null, { aborted: false }); }
  catch (e) { error = e; }
  finally {
    global.fetch = savedFetch;
    if (hadWindow) global.window = savedWindow; else delete global.window;
  }
  return { calls: s.calls, error, result };
}

const bytes = str => new TextEncoder().encode(str);

test("default transport cancels the oversized body and reports a finite error", async () => {
  const { calls, error } = await runTransport({ steps: [{ value: { byteLength: 3 * 1024 * 1024 } }] });
  assert.match(error.message, /too large/);
  assert.equal(calls.cancel, 1, "the oversized stream is cancelled, not left unretired");
  assert.equal(calls.releaseLock, 1);
});

test("default transport retires the stream on invalid UTF-8", async () => {
  const { calls, error } = await runTransport({ steps: [{ value: new Uint8Array([0xff, 0xff]) }] });
  assert.ok(error);
  assert.equal(calls.cancel, 1);
});

test("default transport retires the stream on a read failure", async () => {
  const { calls, error } = await runTransport({ steps: [{ throwRead: true }] });
  assert.match(error.message, /read failed/);
  assert.equal(calls.cancel, 1);
});

test("default transport retires the stream on a malformed JSON body", async () => {
  const { calls, error } = await runTransport({ steps: [{ value: bytes("not json") }, { done: true }] });
  assert.ok(error);
  assert.equal(calls.cancel, 1);
});

test("default transport does not cancel a cleanly read body (only releases the lock)", async () => {
  const { calls, error, result } = await runTransport({ steps: [{ value: bytes('{"rows":[]}') }, { done: true }] });
  assert.equal(error, null);
  assert.deepEqual(result, { httpStatus: 200, body: { rows: [] } });
  assert.equal(calls.cancel, 0, "a clean read is not cancelled");
  assert.equal(calls.releaseLock, 1);
});

test("a rejecting cancel never strands completion (the original error is still reported)", async () => {
  const { calls, error } = await runTransport({ steps: [{ value: { byteLength: 3 * 1024 * 1024 } }], cancelRejects: true });
  assert.match(error.message, /too large/, "the original error propagates, not the cancel rejection");
  assert.equal(calls.cancel, 1);
});

test("a 401 retires the body and throws with the status before reading", async () => {
  const { calls, error } = await runTransport({ steps: [] }, 401);
  assert.equal(error.status, 401);
  assert.equal(calls.cancel, 1, "the body is retired on an auth response too");
});

test("a run change clears stale rows/cursor before recovery; a failed (503) recovery leaves it empty", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(1), row(2)], { cursor: "c2", has_more: true, run_id: "r1" })); await flush();
  const m = f.control.loadMore(); await flush();
  f.pageCalls[1].resolve(page([row(3, { run_id: "r2" })], { cursor: "c3", has_more: true, run_id: "r2" }));
  await m; await flush();
  assert.equal(f.pageCalls[2].cursor, null, "recovery fetches the oldest page of the new run");
  f.pageCalls[2].resolve({ httpStatus: 503, body: { status: { reason: "start_failed" } } }); await flush();
  assert.deepEqual(f.lastRows(), [], "obsolete r1 rows are dropped, not retained through the failed recovery");
  assert.equal(f.lastMeta().has_more, false, "the obsolete has_more is cleared");
  assert.equal(await f.control.loadMore(), false, "the obsolete cursor is not a usable continuation");
  assert.equal(f.lastState(), "unavailable");
});

test("run change then expired recovery ends stale (no in-flight reload implied)", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(1)], { cursor: "c1", has_more: true, run_id: "r1" })); await flush();
  const m = f.control.loadMore(); await flush();
  f.pageCalls[1].resolve(page([row(2, { run_id: "r2" })], { cursor: "c2", has_more: true, run_id: "r2" }));
  await m; await flush();
  f.pageCalls[2].resolve({ httpStatus: 410, body: { reason: "cursor_expired" } }); await flush();
  assert.equal(f.pageCalls.length, 3, "one recovery only");
  assert.equal(f.lastState(), "stale");
  assert.deepEqual(f.lastRows(), []);
});

test("the trim notice stays sticky across a later non-overflowing page and resets on refresh", async () => {
  const f = fixture({ domCap: 3 });
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(1), row(2)], { cursor: "c2", has_more: true })); await flush();
  const m1 = f.control.loadMore(); await flush();
  f.pageCalls[1].resolve(page([row(3), row(4)], { cursor: "c4", has_more: true })); await m1;
  assert.deepEqual(f.lastRows(), [2, 3, 4]);
  assert.equal(f.lastMeta().trimmed, true, "older rows were dropped");
  const m2 = f.control.loadMore(); await flush();
  f.pageCalls[2].resolve(page([], { cursor: "c4", has_more: false })); await m2;   // empty, no new overflow
  assert.equal(f.lastMeta().trimmed, true, "notice stays accurate: older rows are still absent from the view");
  const r = f.control.refresh(); await flush();
  f.pageCalls[3].resolve(page([row(5)], { has_more: false })); await r;
  assert.equal(f.lastMeta().trimmed, false, "a refresh resets the trim state for the fresh view");
});

// ── "Check for newer": bounded forward poll of the held cursor after catching up ─────────────

test("check for newer polls the held cursor and appends rows that arrived after catching up", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(1), row(2)], { cursor: "c2", has_more: false })); await flush();
  assert.equal(f.lastMeta().has_more, false);
  assert.equal(f.lastMeta().can_check_newer, true, "a held cursor allows a forward poll");
  const c = f.control.checkNewer(); await flush();
  assert.equal(f.pageCalls[1].cursor, "c2", "check-newer polls forward from the held cursor, not oldest");
  f.pageCalls[1].resolve(page([row(3)], { cursor: "c3", has_more: false }));
  assert.equal(await c, true);
  assert.deepEqual(f.lastRows(), [1, 2, 3], "newly-arrived rows append; no restart from oldest");
});

test("check for newer with nothing new stays caught up and appends nothing", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(1)], { cursor: "c1", has_more: false })); await flush();
  const c = f.control.checkNewer(); await flush();
  f.pageCalls[1].resolve(page([], { cursor: "c1", has_more: false }));
  await c;
  assert.deepEqual(f.lastRows(), [1], "no new rows -> no change");
  assert.equal(f.lastMeta().has_more, false);
});

test("check for newer is a no-op before any load (no held cursor)", async () => {
  const f = fixture();
  assert.equal(await f.control.checkNewer(), false);
  assert.equal(f.pageCalls.length, 0, "no forward poll without a cursor");
});

test("a second check-for-newer supersedes the first in-flight read", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(1)], { cursor: "c1", has_more: false })); await flush();
  const first = f.control.checkNewer(); await flush();
  const second = f.control.checkNewer(); await flush();
  assert.equal(await first, false, "the superseded poll resolves false");
  assert.ok(f.pageCalls[1].signal.aborted, "and its request is aborted");
  f.pageCalls[2].resolve(page([row(2)], { cursor: "c2", has_more: false }));
  assert.equal(await second, true);
  assert.deepEqual(f.lastRows(), [1, 2]);
});

test("check for newer on an expired cursor recovers once from the oldest page", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(5)], { cursor: "c5", has_more: false })); await flush();
  const c = f.control.checkNewer(); await flush();
  f.pageCalls[1].resolve({ httpStatus: 410, body: { reason: "cursor_expired" } });
  await c; await flush();
  assert.ok(f.states.some(s => s.state === "expired"), "expiry is surfaced");
  assert.equal(f.pageCalls[2].cursor, null, "the single recovery re-fetches from the oldest page");
  f.pageCalls[2].resolve(page([row(1)], { cursor: "c1", has_more: false })); await flush();
  assert.deepEqual(f.lastRows(), [1]);
});

test("check for newer is a no-op when history is disabled (cursor cleared)", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([], { mode: "disabled", cursor: null, has_more: false, earliest_seq: null }));
  await flush();
  assert.equal(f.lastMeta().can_check_newer, false, "disabled clears the held cursor");
  assert.equal(await f.control.checkNewer(), false);
});

test("server retention: earliest_seq>1 surfaces evicted + window start, distinct from client trim", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(204), row(205)], { cursor: "c205", has_more: false, earliest_seq: 204 }));
  await flush();
  assert.equal(f.lastMeta().evicted, true, "server eviction surfaced when earliest_seq > 1");
  assert.equal(f.lastMeta().retained_from, 204, "window start is the earliest retained seq, not a lost count");
  assert.equal(f.lastMeta().trimmed, false, "the client DOM-trim notice stays independent");
});

test("server retention: earliest_seq==1 is not evicted (window starts at the first report)", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(1), row(2)], { cursor: "c2", has_more: false, earliest_seq: 1 }));
  await flush();
  assert.equal(f.lastMeta().evicted, false);
  assert.equal(f.lastMeta().retained_from, null);
});

test("server retention clears when history is disabled", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(210)], { cursor: "c210", has_more: false, earliest_seq: 210 })); await flush();
  assert.equal(f.lastMeta().evicted, true);
  f.control.refresh(); await flush();
  f.pageCalls[1].resolve(page([], { mode: "disabled", cursor: null, has_more: false, earliest_seq: null, reason: "disabled" }));
  await flush();
  assert.equal(f.lastMeta().evicted, false, "disabled clears the retention notice");
  assert.equal(f.lastMeta().retained_from, null);
});

test("server retention updates after a 410 recovery to an advanced window", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(1), row(2)], { cursor: "c2", has_more: true, earliest_seq: 1 })); await flush();
  assert.equal(f.lastMeta().evicted, false);
  const m = f.control.loadMore(); await flush();
  f.pageCalls[1].resolve({ httpStatus: 410, body: { reason: "cursor_expired", earliest_seq: 50 } });
  await m; await flush();
  f.pageCalls[2].resolve(page([row(50), row(51)], { cursor: "c51", has_more: false, earliest_seq: 50 })); await flush();
  assert.equal(f.lastMeta().evicted, true, "the advanced retained window now reports eviction");
  assert.equal(f.lastMeta().retained_from, 50);
});

test("server retention: an older loaded row is kept when the server window advances past it", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  // caught up holding row 1 at the oldest boundary (earliest_seq 1); the cursor stays valid
  f.pageCalls[0].resolve(page([row(1)], { cursor: "c1", has_more: false, earliest_seq: 1 })); await flush();
  assert.equal(f.lastMeta().evicted, false);
  // a forward poll returns row 2 and the SERVER earliest advances to 2, but cursor 1 (=earliest-1)
  // is still valid, so row 2 appends and the older row 1 is retained in the view
  const c = f.control.checkNewer(); await flush();
  f.pageCalls[1].resolve(page([row(2)], { cursor: "c2", has_more: false, earliest_seq: 2 }));
  assert.equal(await c, true);
  assert.deepEqual(f.lastRows(), [1, 2], "the older loaded row (#1) is retained, never dropped to fit a label");
  assert.equal(f.lastMeta().evicted, true, "eviction surfaced: the server window advanced past #1");
  assert.equal(f.lastMeta().retained_from, 2,
    "retained_from is the SERVER boundary (#2), not the displayed first row (#1)");
});

test("server retention and client trim coexist independently (both flags true at once)", async () => {
  const f = fixture({ domCap: 2 });
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(10), row(11)], { cursor: "c11", has_more: true, earliest_seq: 10 }));
  await flush();
  assert.equal(f.lastMeta().evicted, true, "server evicted (earliest_seq 10 > 1)");
  assert.equal(f.lastMeta().trimmed, false, "no client trim yet (2 rows == cap)");
  const m = f.control.loadMore(); await flush();
  f.pageCalls[1].resolve(page([row(12)], { cursor: "c12", has_more: false, earliest_seq: 10 }));
  await m;
  assert.deepEqual(f.lastRows(), [11, 12], "client cap trimmed the oldest loaded row (#10)");
  assert.equal(f.lastMeta().trimmed, true, "client-trim notice");
  assert.equal(f.lastMeta().evicted, true, "server-retention notice is independent and still true");
  assert.equal(f.lastMeta().retained_from, 10);
});

test("server retention: a failed read (503) preserves the prior evicted view and its notice meta", async () => {
  const f = fixture();
  f.control.refresh(); await flush();
  f.pageCalls[0].resolve(page([row(204)], { cursor: "c204", has_more: false, earliest_seq: 204 })); await flush();
  assert.equal(f.lastMeta().evicted, true);
  assert.equal(f.lastMeta().retained_from, 204);
  const deliveriesBefore = f.deliveries.length;
  // a subsequent refresh whose read fails 503: the reader notifies unavailable, preserves the rows,
  // and does NOT re-present -- so the last delivered meta is unchanged. The UI must therefore not
  // pre-hide the retention/trim notices on refresh (they would desync from the preserved rows).
  f.control.refresh(); await flush();
  f.pageCalls[1].resolve({ httpStatus: 503, body: { reason: "unavailable" } }); await flush();
  assert.equal(f.lastState(), "unavailable");
  assert.equal(f.deliveries.length, deliveriesBefore, "no new delivery on 503; the prior view is preserved");
  assert.equal(f.lastMeta().evicted, true, "the retention meta stays as last delivered (not cleared)");
  assert.equal(f.lastMeta().retained_from, 204);
});
