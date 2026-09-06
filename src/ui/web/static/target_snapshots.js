/* Ordered, bounded reads of the shared target pool. No device operations. */
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.CCTargetSnapshots = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const MAX_BYTES = 8 * 1024 * 1024;
  const MAX_ROWS = 5000;
  function validTimestamp(value) {
    if (typeof value !== "string" || value.length > 40) return false;
    const m = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d{1,6})?(?:Z|[+-](\d\d):(\d\d))$/.exec(value);
    if (!m) return false;
    const year=Number(m[1]), month=Number(m[2]), day=Number(m[3]);
    const leap=year%4===0 && (year%100!==0 || year%400===0);
    const days=[31,leap?29:28,31,30,31,30,31,31,30,31,30,31];
    return year>0 && month>=1 && month<=12 && day>=1 && day<=days[month-1] &&
      Number(m[4])<24 && Number(m[5])<60 && Number(m[6])<60 && (!m[7] || (Number(m[7])<24 && Number(m[8])<60));
  }

  function validate(rows) {
    if (!Array.isArray(rows) || rows.length > MAX_ROWS) throw new Error("Invalid target list");
    return rows.map(function (row) {
      if (!row || typeof row !== "object" || Array.isArray(row) ||
          typeof row.target_type !== "string" || !row.target_type || row.target_type.length > 32) {
        throw new Error("Invalid target record");
      }
      const copy = { target_type: row.target_type };
      // The API's timestamp is first-seen time, distinct from last_seen.
      if (row.timestamp != null && !validTimestamp(row.timestamp)) throw new Error("Invalid target timestamp");
      copy.timestamp = row.timestamp == null ? "" : row.timestamp;
      ["mac", "ssid", "vendor", "encryption", "device_source", "last_seen"].forEach(function (key) {
        if (row[key] != null && typeof row[key] !== "string") throw new Error("Invalid target text");
        copy[key] = row[key] == null ? "" : row[key];
      });
      ["rssi", "channel"].forEach(function (key) {
        if (row[key] != null && (typeof row[key] !== "number" || !Number.isFinite(row[key]))) {
          throw new Error("Invalid target number");
        }
        copy[key] = row[key] == null ? null : row[key];
      });
      return copy;
    });
  }

  function bleSignal(value) {
    // The current Target model uses zero for an omitted signal reading.
    return typeof value === "number" && Number.isFinite(value) && value !== 0 ? value : null;
  }

  async function transport(signal) {
    if (signal.aborted) throw new Error("Target request retired");
    let reader = null;
    function releaseBody(cancel) {
      if (!reader) return;
      const owned = reader;
      reader = null;
      if (cancel) void owned.cancel().catch(function () {});
      owned.releaseLock();
    }
    function abortBody() { releaseBody(true); }
    signal.addEventListener("abort", abortBody, { once: true });
    const decoder = new TextDecoder("utf-8", { fatal: true });
    let bytes = 0, text = "", complete = false;
    try {
      const response = await fetch("/api/targets", {
        signal, credentials: "same-origin", redirect: "error", cache: "no-store",
        headers: { "X-CSRF-Token": window.CSRF_TOKEN || "" },
      });
      // Headers may arrive after retirement, even when a transport ignored abort.
      if (signal.aborted || !response.ok) {
        if (response.body) void response.body.cancel().catch(function () {});
        const error = new Error("Target request failed");
        if (!signal.aborted) error.status = response.status;
        throw error;
      }
      reader = response.body.getReader();
      for (;;) {
        const part = await reader.read();
        if (signal.aborted) throw new Error("Target request retired");
        if (part.done) break;
        bytes += part.value.byteLength;
        if (bytes > MAX_BYTES) throw new Error("Target response is too large");
        text += decoder.decode(part.value, { stream: true });
      }
      text += decoder.decode();
      complete = true;
      return JSON.parse(text);
    } finally {
      signal.removeEventListener("abort", abortBody);
      releaseBody(!complete);
    }
  }

  function create(options) {
    const load = options.load || transport;
    const later = options.setTimeout || setTimeout;
    const cancelTimer = options.clearTimeout || clearTimeout;
    const timeout = options.timeoutMs === undefined ? 15000 : options.timeoutMs;
    if (!Number.isInteger(timeout) || timeout < 100 || timeout > 120000) throw new Error("Invalid timeout");
    let generation = 0, active = null, retired = false;

    function notifyStatus(state, token) {
      if (token !== generation || retired) return;
      // Status rendering is an observer. Its failure must not strand the data request.
      try { options.onStatus(state); } catch (error) { /* The next refresh can still proceed. */ }
    }

    function refresh(replace) {
      if (retired) return Promise.resolve(false);
      if (active && !replace) return active.promise;
      const token = ++generation;
      if (active) active.cancel();
      // Cancellation can synchronously refresh or suspend through an abort callback.
      // A newer generation owns its slot before this older invocation continues.
      if (token !== generation || retired) return Promise.resolve(false);
      const abort = new AbortController();
      const operation = { promise: null, cancel: null };
      let finished = false, timer = null, resolve;
      operation.promise = new Promise(function (done) { resolve = done; });
      function finish(ok, value) {
        if (finished) return;
        finished = true;
        if (timer !== null) cancelTimer(timer);
        if (active === operation) active = null;
        if (token !== generation || retired) { resolve(false); return; }
        if (ok) {
          try { value = validate(value); }
          catch (error) { ok = false; value = error; }
        }
        let state = "error";
        try {
          if (ok) {
            options.onSnapshot(value, { kind: "snapshot", generation: token });
            state = "fresh";
          } else if (value && (value.status === 401 || value.status === 403)) {
            options.onSnapshot([], { kind: "auth-loss", generation: token });
            state = "unauthorized";
          }
        } catch (error) { ok = false; }
        // A consumer can synchronously refresh or suspend while accepting a snapshot.
        // Its new generation owns status too; the older completion cannot label it fresh.
        notifyStatus(state, token);
        resolve(ok && token === generation && !retired);
      }
      operation.cancel = function () { abort.abort(); finish(false, null); };
      active = operation;
      notifyStatus("loading", token);
      if (finished || token !== generation || retired) return operation.promise;
      timer = later(function () {
        abort.abort();
        finish(false, new Error("Target request timed out"));
      }, timeout);
      Promise.resolve().then(function () {
        if (!finished) return load(abort.signal);
      }).then(function (rows) { finish(true, rows); }, function (error) { finish(false, error); });
      return operation.promise;
    }

    return {
      refresh,
      suspend: function () {
        retired = true;
        generation += 1;
        const previous = active;
        active = null;
        // A cancellation callback may resume and install its own request.
        if (previous) previous.cancel();
      },
      resume: function () { retired = false; return refresh(); },
    };
  }
  return { create, validate, validTimestamp, bleSignal, transport, MAX_BYTES, MAX_ROWS };
});
