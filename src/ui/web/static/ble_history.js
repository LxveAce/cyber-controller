/* Bounded, read-only reader of the memory-only BLE session-history window.
 *
 * The /api/ble-history journal is OLDEST-first with a forward opaque cursor: no cursor returns the
 * oldest retained page, and the returned cursor loads the NEXT (newer) page. This reader never
 * reverses the cursor or claims newest-page behaviour. Rows are passive reports with no target or
 * selection authority; the reader only reads. History is memory-only and cleared on restart, so a
 * run_id change or a 410 restarts from the oldest still-available page (at most one automatic
 * recovery per user action). It holds at most domCap rows, dropping the oldest when it overflows.
 */
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) {
    module.exports = factory(require("./target_snapshots.js").validTimestamp);
  } else {
    root.CCBLEHistory = factory(root.CCTargetSnapshots.validTimestamp);
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function (validTimestamp) {
  "use strict";
  const MAX_BYTES = 2 * 1024 * 1024, MAX_ROWS = 256;
  const record = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const text = (value, max) => typeof value === "string" && value.length <= max * 2 &&
    Array.from(value).length <= max;

  // A finite, escaped, display-only view of one journal row. Unknown/optional fields are normalised;
  // an address is never carried forward (a row is a report, not a selectable device).
  function validate(value) {
    if (!record(value) || !record(value.status) || !Array.isArray(value.rows) ||
        value.rows.length > MAX_ROWS || typeof value.has_more !== "boolean" ||
        !(value.cursor === null || typeof value.cursor === "string") ||
        !(value.earliest_seq === null || Number.isInteger(value.earliest_seq))) {
      throw new Error("Invalid BLE history page");
    }
    const status = value.status;
    if (typeof status.effective_mode !== "string" && status.effective_mode !== null) {
      throw new Error("Invalid BLE history status");
    }
    const rows = value.rows.map(function (row) {
      if (!record(row) || !Number.isInteger(row.seq) || row.seq < 0 || !text(row.label, 256) ||
          !row.label || !Number.isInteger(row.rssi) || row.rssi < -128 || row.rssi > 127 ||
          typeof row.kind !== "string" || !record(row.source) || !text(row.source.port, 512) ||
          !validTimestamp(row.observed_at) || typeof row.addressable !== "boolean") {
        throw new Error("Invalid BLE history row");
      }
      // The render never surfaces an address (a row is not a device); do not carry it forward.
      return { seq: row.seq, label: row.label, rssi: row.rssi, kind: row.kind,
        source_port: row.source.port, observed_at: row.observed_at, addressable: row.addressable };
    });
    return { status: status, rows: rows, cursor: value.cursor, has_more: value.has_more,
      earliest_seq: value.earliest_seq, run_id: typeof status.run_id === "string" ? status.run_id : null };
  }

  // Default transport: a single bounded GET; reads the body on 200 AND on 400/410/503 (the route
  // returns a finite JSON body for each) so the controller can map the outcome to a state.
  // Best-effort, fire-and-forget cancellation of a stream body or reader: a rejecting or hanging
  // cancel can never strand the caller's completion (we do not await it).
  function retire(streamOrReader) {
    if (!streamOrReader) return;
    try { Promise.resolve(streamOrReader.cancel()).catch(function () {}); }
    catch (_) { /* some engines throw synchronously; ignore */ }
  }

  async function transport(cursor, signal) {
    if (signal.aborted) throw new Error("BLE history request retired");
    const query = cursor === null ? "?limit=50" : "?cursor=" + encodeURIComponent(cursor) + "&limit=50";
    const response = await fetch("/api/ble-history" + query, {
      signal, credentials: "same-origin", redirect: "error", cache: "no-store",
      headers: { "X-CSRF-Token": window.CSRF_TOKEN || "" },
    });
    if (response.status === 401 || response.status === 403) {
      retire(response.body);
      const error = new Error("BLE history request unauthorized");
      error.status = response.status;
      throw error;
    }
    const reader = response.body && typeof response.body.getReader === "function"
      ? response.body.getReader() : null;
    if (!reader) { retire(response.body); throw new Error("Missing BLE history body"); }
    const decoder = new TextDecoder("utf-8", { fatal: true });
    let bytes = 0, decoded = "", complete = false;
    try {
      for (;;) {
        const part = await reader.read();   // a read failure or an abort rejects here
        if (part.done) break;
        bytes += part.value.byteLength;
        if (bytes > MAX_BYTES) throw new Error("BLE history response is too large");
        decoded += decoder.decode(part.value, { stream: true });   // fatal: invalid UTF-8 throws
      }
      const result = JSON.parse(decoded + decoder.decode());        // a malformed body throws
      complete = true;
      return { httpStatus: response.status, body: result };
    } finally {
      // Retire the stream on oversize / invalid UTF-8 / read or parse failure / abort: cancel the
      // reader (best-effort, not awaited) BEFORE releasing the lock so the response body is not left
      // unretired. A clean read only releases the already-drained lock.
      if (!complete) retire(reader);
      try { reader.releaseLock(); } catch (_) { /* engines may defer during a pending read */ }
    }
  }

  function create(options) {
    const load = options.fetchPage || transport, deliver = options.onPage, status = options.onStatus;
    const later = options.setTimeout || setTimeout, cancelTimer = options.clearTimeout || clearTimeout;
    const now = options.now || function () { return performance.now(); };
    const timeout = options.timeoutMs === undefined ? 15000 : options.timeoutMs;
    const domCap = options.domCap === undefined ? 500 : options.domCap;
    if (!Number.isInteger(timeout) || timeout < 100 || timeout > 120000 ||
        !Number.isInteger(domCap) || domCap < 1 || domCap > 5000 ||
        typeof deliver !== "function" || typeof status !== "function") {
      throw new Error("Invalid BLE history reader options");
    }
    let generation = 0, active = null, suspended = false;
    let cursor = null, runId = null, rows = [], hasMore = false, recovered = false, everTrimmed = false;

    function notify(state, token, extra) {
      if (token !== generation || suspended) return;
      try { status(state, extra || {}); } catch (_) { /* a status observer cannot strand the read */ }
    }
    function present(token) {
      if (token !== generation || suspended) return;
      // `trimmed` is sticky for the view's lifetime once any older loaded rows were dropped, so a
      // later non-overflowing (or empty) page never wrongly clears the "older rows trimmed" notice.
      // can_check_newer: a forward poll of the held cursor is possible (a cursor exists). The UI
      // offers it once caught up (has_more false) to pull reports that arrived AFTER this page,
      // without restarting from the oldest page.
      try {
        deliver(rows.slice(), { has_more: hasMore, trimmed: everTrimmed, count: rows.length,
                                can_check_newer: cursor !== null });
      }
      catch (_) { /* a render observer cannot strand the read */ }
    }

    // Drop the whole loaded view (rows + cursor + has_more + trim state). Used before a recovery so a
    // failed recovery can never leave stale rows or an obsolete cursor usable as a continuation.
    function clearView(token) {
      rows = []; cursor = null; hasMore = false; everTrimmed = false;
      present(token);
    }

    // append=false replaces (refresh from oldest); append=true adds the next (newer) page.
    function apply(page, append, token) {
      if (!append || page.run_id !== runId) { rows = []; everTrimmed = false; }   // restart clears view
      runId = page.run_id;
      cursor = page.cursor;
      hasMore = page.has_more;
      for (const row of page.rows) rows.push(row);
      if (rows.length > domCap) { rows = rows.slice(rows.length - domCap); everTrimmed = true; }
      present(token);
    }

    // Map one finite outcome to {handled, recover}. `recover` asks finish() to restart from the
    // oldest page once (a 410 or a run change); the single-recovery bound lives in finish().
    function outcome(result, append, token) {
      const body = result.body, http = result.httpStatus;
      if (http === 200) {
        const view = validate(body);
        if (view.status.effective_mode === "memory") {
          if (append && runId !== null && view.run_id !== runId) {   // restarted mid-session
            clearView(token);   // drop the obsolete run's rows + cursor BEFORE the bounded recovery
            return { handled: true, recover: true };
          }
          apply(view, append, token);
          notify(rows.length ? "fresh" : "empty", token, { earliest_seq: view.earliest_seq });
          return { handled: true, recover: false };
        }
        clearView(token);   // disabled / non-memory: finite, empty, off
        notify("disabled", token, { reason: view.status.reason || null });
        return { handled: true, recover: false };
      }
      if (http === 410) {   // retained window moved past our position: drop the stale view first
        clearView(token);
        return { handled: true, recover: true };
      }
      if (http === 503) { notify("unavailable", token, { reason: reasonOf(body) }); return { handled: true, recover: false }; }
      notify("error", token, { reason: reasonOf(body) });   // 400 and anything else: finite, manual
      return { handled: false, recover: false };
    }
    function reasonOf(body) {
      return record(body) && typeof body.reason === "string" ? body.reason
        : record(body) && record(body.status) && typeof body.status.reason === "string"
          ? body.status.reason : null;
    }

    // One single-flight fetch. fromOldest=true clears the cursor (refresh); otherwise load the next
    // page with the held cursor. A superseded call never overwrites a newer read's slot.
    function start(fromOldest, parentToken, userAction) {
      if (suspended) return Promise.resolve(false);
      if (active) active.cancel();
      const token = parentToken !== undefined ? parentToken : ++generation;
      if (token !== generation || suspended) return Promise.resolve(false);
      if (userAction) recovered = false;   // a fresh user action re-arms the single auto-recovery
      const useCursor = fromOldest ? null : cursor;
      const abort = new AbortController(), deadline = now() + timeout;
      const op = { promise: null, cancel: null };
      let finished = false, timer = null, resolve;
      op.promise = new Promise(done => { resolve = done; });
      function finish(ok, result) {
        if (finished) return;
        finished = true;
        if (timer !== null) cancelTimer(timer);
        if (active === op) active = null;
        if (token !== generation || suspended) { resolve(false); return; }
        if (now() >= deadline) { ok = false; result = null; abort.abort(); }
        let handled = false, recover = false;
        try {
          if (ok) { const r = outcome(result, !fromOldest, token); handled = r.handled; recover = r.recover; }
          else if (result && (result.status === 401 || result.status === 403)) notify("unauthorized", token);
          else notify("error", token, {});
        } catch (_) { notify("error", token, {}); }
        resolve(handled && token === generation && !suspended);
        // A 410 or a run change asks to recover from the oldest page, but at most ONCE per user
        // action (a fresh refresh/loadMore/resume re-arms it by clearing `recovered`). While the one
        // recovery is in flight, show the transient "expired" (reloading) state; once it is spent,
        // end in the terminal "stale" state (manual Refresh) — never implying an in-flight reload
        // that no longer exists. The view is already cleared (clearView), so neither state shows
        // stale rows or leaves an obsolete cursor usable.
        if (recover && !suspended && token === generation) {
          if (!recovered) {
            recovered = true;
            notify("expired", token);
            start(true, undefined, false);
          } else {
            notify("stale", token);
          }
        }
      }
      op.cancel = function () { abort.abort(); finish(false, null); };
      active = op;
      notify("loading", token);
      if (finished || token !== generation || suspended) return op.promise;
      if (now() >= deadline) { abort.abort(); finish(false, null); return op.promise; }
      timer = later(function () { abort.abort(); finish(false, null); }, Math.max(0, deadline - now()));
      Promise.resolve().then(function () {
        if (!finished) return load(useCursor, abort.signal);
      }).then(value => finish(true, value), error => finish(false, error));
      return op.promise;
    }

    return {
      // Reset to the oldest retained page (a user action).
      refresh: function () { if (suspended) return Promise.resolve(false); return start(true, undefined, true); },
      // Load the next (newer) page after the held cursor (a user action).
      loadMore: function () {
        if (suspended || !hasMore || cursor === null) return Promise.resolve(false);
        return start(false, undefined, true);
      },
      // Poll the held forward cursor for reports that arrived AFTER catching up (has_more=false),
      // appending only genuinely newer rows. Distinct from refresh (which restarts from the oldest
      // page): this is a bounded, user-triggered forward read of existing journal data from the
      // cursor we already hold — never a scan/device command or a new page we don't have a cursor
      // for. It shares the single-flight slot, run/expiry invalidation and cancellation of loadMore.
      checkNewer: function () {
        if (suspended || cursor === null) return Promise.resolve(false);
        return start(false, undefined, true);
      },
      hasMore: function () { return hasMore; },
      suspend: function () {
        suspended = true; generation++;
        const previous = active; active = null;
        if (previous) previous.cancel();
      },
      resume: function () { suspended = false; return start(true, undefined, true); },
      // Un-suspend after a bfcache/persisted pageshow WITHOUT reloading: clears the freeze so user
      // actions (expand/refresh/loadMore) work again, preserving the already-loaded rows + cursor
      // (the restored DOM already shows them). A read cancelled by the preceding suspend left the UI
      // on "loading"/aria-busy; settle it to a truthful finite state ("fresh" if rows were already
      // loaded, else the neutral "idle") so nothing implies a request that no longer exists. A later
      // refresh/loadMore re-arms the auto-recovery.
      wake: function () {
        suspended = false;
        const token = generation;
        present(token);
        notify(rows.length ? "fresh" : "idle", token);
      },
    };
  }
  return { validate, transport, create, MAX_BYTES, MAX_ROWS };
});
