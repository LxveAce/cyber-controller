/* Follow one manual update operation under the Updates card's total deadline. */
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) module.exports = factory(require("./updates_card.js"));
  else root.CCUpdatesTransport = factory(root.CCUpdatesCard);
})(typeof globalThis !== "undefined" ? globalThis : this, function (card) {
  "use strict";
  const record = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const identity = value => typeof value === "string" && value.length === 32 && /^[0-9a-f]{32}$/.test(value);

  function create(options) {
    const request = options.fetch, csrf = options.csrfToken;
    const later = options.setTimeout || setTimeout, cancel = options.clearTimeout || clearTimeout;
    if (typeof request !== "function" || typeof csrf !== "function" || !card ||
        typeof card.parseReply !== "function") throw new Error("Invalid update transport options");

    // The existing card owns the 30-second deadline and aborts this entire operation,
    // including a hung response body. Individual polls never reset that deadline.
    function check(signal) {
      return new Promise(function (resolve, reject) {
        let done = false, timer = null, runtime = null, operation = null, previous = null;
        const aborted = () => finish(new Error("Update check interrupted"));
        function finish(error, value) {
          if (done) return;
          done = true;
          const pending = timer;
          timer = null;
          try { if (pending !== null) cancel(pending); } catch (_) { /* Results remain fenced. */ }
          try { signal.removeEventListener("abort", aborted); } catch (_) { /* Finish remains final. */ }
          if (error) reject(error); else resolve(value);
        }
        function live() { return !done && !signal.aborted; }
        function poll() {
          if (!live()) { aborted(); return; }
          send("/api/updates/status?runtime_id=" + runtime + "&operation_id=" + operation, false);
        }
        function accept(value, admission) {
          if (!live()) { aborted(); return; }
          if (!record(value) || value.schema_version !== 2 || !identity(value.runtime_id) ||
              !identity(value.operation_id)) throw new Error("Invalid update operation");
          if (admission) {
            if (value.accepted !== true || !["started", "coalesced"].includes(value.reason)) {
              throw new Error("Update check was not accepted");
            }
            runtime = value.runtime_id;
            operation = value.operation_id;
          } else if (value.runtime_id !== runtime || value.operation_id !== operation) {
            throw new Error("Update operation changed");
          }
          if (value.error !== undefined || value.retirement_reason !== null) {
            throw new Error("Update operation failed");
          }
          if (value.phase === "completed") {
            if (!record(value.result) || !Object.prototype.hasOwnProperty.call(value.result, "current") ||
                (value.result.status === "OFFLINE" && value.result.ok !== false)) {
              throw new Error("Invalid completed update result");
            }
            card.parseReply(value.result);
            finish(null, value.result);
            return;
          }
          if (!["queued", "checking"].includes(value.phase) || value.result !== null ||
              (previous === "checking" && value.phase === "queued")) {
            throw new Error("Invalid update phase");
          }
          previous = value.phase;
          if (!live()) { aborted(); return; }
          timer = later(function () { timer = null; poll(); }, 1000);
        }
        function send(url, admission) {
          Promise.resolve().then(function () {
            if (!live()) throw new Error("Update check interrupted");
            const settings = { method: admission ? "POST" : "GET", signal: signal,
              credentials: "same-origin", cache: "no-store", redirect: "error" };
            if (admission) {
              settings.headers = { "Content-Type": "application/json", "X-CSRF-Token": csrf() };
              settings.body = JSON.stringify({ schema_version: 2 });
            }
            if (!live()) throw new Error("Update check interrupted");
            return request(url, settings);
          }).then(function (response) {
            if (!live()) throw new Error("Update check interrupted");
            if (!response || response.status !== (admission ? 202 : 200) || response.ok !== true) {
              throw new Error("Update request failed");
            }
            return response.json();
          }).then(function (value) {
            if (live()) accept(value, admission);
          }).catch(function (error) { finish(error); });
        }
        if (!signal || typeof signal.addEventListener !== "function" ||
            typeof signal.removeEventListener !== "function" || typeof signal.aborted !== "boolean") {
          finish(new Error("Update check requires an abort signal"));
          return;
        }
        signal.addEventListener("abort", aborted, { once: true });
        if (signal.aborted) { aborted(); return; }
        send("/api/updates/check", true);
      });
    }
    return Object.freeze({ check: check });
  }
  return Object.freeze({ create: create });
});
