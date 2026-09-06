/* Bounded reads of the current BLE report window. Reports have no target or device authority. */
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) module.exports = factory(require("./target_snapshots.js").validTimestamp);
  else root.CCBLEReports = factory(root.CCTargetSnapshots.validTimestamp);
})(typeof globalThis !== "undefined" ? globalThis : this, function (validTimestamp) {
  "use strict";
  const MAX_BYTES = 2 * 1024 * 1024, MAX_ROWS = 200;
  const counter = value => typeof value === "string" && /^[1-9][0-9]{0,63}$/.test(value);
  const text = (value, max) => typeof value === "string" && value.length <= max * 2 && Array.from(value).length <= max;
  const record = value => value !== null && typeof value === "object" && !Array.isArray(value);

  function validate(value) {
    if (!record(value) || typeof value.available !== "boolean" || !Array.isArray(value.observations) ||
        value.observations.length > MAX_ROWS || (!value.available && value.observations.length)) {
      throw new Error("Invalid BLE report window");
    }
    const observations = value.observations.map(function (row) {
      if (!record(row) || !text(row.label, 256) || !row.label || !text(row.device_source, 512) ||
          !Number.isInteger(row.rssi) || row.rssi < -128 || row.rssi > 127 ||
          typeof row.label_truncated !== "boolean" || row.addressable !== false ||
          !counter(row.observation_id) || !counter(row.connection_epoch) || !counter(row.scan_epoch) ||
          !validTimestamp(row.observed_at) ||
          !(row.format === "live" && row.reported_index === null || row.format === "list" &&
            Number.isInteger(row.reported_index) && row.reported_index >= 0 && row.reported_index <= 999999999)) {
        throw new Error("Invalid BLE report");
      }
      return { observation_id: row.observation_id, label: row.label, rssi: row.rssi,
        reported_index: row.reported_index, format: row.format, label_truncated: row.label_truncated,
        addressable: false, device_source: row.device_source, connection_epoch: row.connection_epoch,
        scan_epoch: row.scan_epoch, observed_at: row.observed_at };
    });
    return { available: value.available, observations };
  }

  // Cleanup never owns the controller's request slot or replaces its original outcome.
  function cancelBody(body) {
    try { Promise.resolve(body.cancel()).catch(function () {}); } catch (_) { /* Best effort. */ }
  }
  async function transport(signal) {
    if (signal.aborted) throw new Error("BLE report request retired");
    let reader = null, cancelled = false, complete = false;
    function releaseBody(cancel) {
      if (!reader) return;
      if (cancel && !cancelled) { cancelled = true; cancelBody(reader); }
      try { reader.releaseLock(); reader = null; }
      catch (_) { /* Older engines can refuse during a pending read; retry when it settles. */ }
    }
    function abortBody() { signal.removeEventListener("abort", abortBody); releaseBody(true); }
    signal.addEventListener("abort", abortBody, { once: true });
    const decoder = new TextDecoder("utf-8", { fatal: true });
    let bytes = 0, decoded = "";
    try {
      const response = await fetch("/api/ble-observations", {
        signal, credentials: "same-origin", redirect: "error", cache: "no-store",
        headers: { "X-CSRF-Token": window.CSRF_TOKEN || "" },
      });
      if (signal.aborted || !response.ok) {
        if (response.body) cancelBody(response.body);
        const error = new Error("BLE report request failed");
        if (!signal.aborted) error.status = response.status;
        throw error;
      }
      if (!response.body || typeof response.body.getReader !== "function") throw new Error("Missing BLE report body");
      reader = response.body.getReader();
      if (signal.aborted) throw new Error("BLE report request retired");
      for (;;) {
        let part;
        try { part = await reader.read(); }
        finally { if (signal.aborted) releaseBody(true); }
        if (signal.aborted) throw new Error("BLE report request retired");
        if (part.done) break;
        bytes += part.value.byteLength;
        if (bytes > MAX_BYTES) throw new Error("BLE report response is too large");
        decoded += decoder.decode(part.value, { stream: true });
      }
      decoded += decoder.decode();
      const result = JSON.parse(decoded);
      complete = true;
      return result;
    } finally {
      signal.removeEventListener("abort", abortBody);
      releaseBody(!complete);
    }
  }

  // Mirrors the separately tested target reader's ownership, without sharing report/target state.
  function create(options) {
    const load = options.load || transport, deliver = options.onReports, status = options.onStatus;
    const later = options.setTimeout || setTimeout, cancelTimer = options.clearTimeout || clearTimeout;
    const now = options.now || function () { return performance.now(); };
    const timeout = options.timeoutMs === undefined ? 15000 : options.timeoutMs;
    if (!Number.isInteger(timeout) || timeout < 100 || timeout > 120000 ||
        typeof deliver !== "function" || typeof status !== "function") throw new Error("Invalid BLE report reader options");
    let generation = 0, active = null, suspended = false;
    function notify(state, token) {
      if (token !== generation || suspended) return;
      try { status(state); } catch (_) { /* A status observer cannot strand the read. */ }
    }
    function refresh(replace) {
      if (suspended) return Promise.resolve(false);
      if (active && !replace) return active.promise;
      const token = ++generation;
      if (active) active.cancel();
      // Abort listeners and stream cancellation can synchronously start a newer read.
      // The superseded caller must not allocate or overwrite that read's request slot.
      if (token !== generation || suspended) return Promise.resolve(false);
      const abort = new AbortController(), deadline = now() + timeout;
      const operation = { promise: null, cancel: null };
      let finished = false, timer = null, resolve;
      operation.promise = new Promise(done => { resolve = done; });
      function finish(ok, value) {
        if (finished) return;
        finished = true;
        if (timer !== null) cancelTimer(timer);
        if (active === operation) active = null;
        if (token !== generation || suspended) { resolve(false); return; }
        if (now() >= deadline) { ok = false; value = null; abort.abort(); }
        if (ok) {
          try { value = validate(value); } catch (error) { ok = false; value = error; }
          if (now() >= deadline) { ok = false; value = null; abort.abort(); }
        }
        let state = "error";
        try {
          if (ok) {
            state = value.available ? "fresh" : "unavailable";
            deliver(value, { kind: "snapshot", generation: token });
          } else if (value && (value.status === 401 || value.status === 403)) {
            deliver({ available: false, observations: [] }, { kind: "auth-loss", generation: token });
            state = "unauthorized";
          }
        } catch (_) { ok = false; state = "error"; }
        notify(state, token);
        resolve(ok && token === generation && !suspended);
      }
      operation.cancel = function () { abort.abort(); finish(false, null); };
      active = operation;
      notify("loading", token);
      if (finished || token !== generation || suspended) return operation.promise;
      if (now() >= deadline) { abort.abort(); finish(false, null); return operation.promise; }
      timer = later(function () { abort.abort(); finish(false, null); }, Math.max(0, deadline - now()));
      Promise.resolve().then(function () {
        if (!finished) return load(abort.signal);
      }).then(value => finish(true, value), error => finish(false, error));
      return operation.promise;
    }
    return { refresh,
      suspend: function () {
        suspended = true; generation++;
        // A cancellation callback may resume; it must see an empty request slot.
        const previous = active; active = null;
        if (previous) previous.cancel();
      },
      resume: function () { suspended = false; return refresh(); } };
  }
  return { validate, transport, create, MAX_BYTES, MAX_ROWS };
});
