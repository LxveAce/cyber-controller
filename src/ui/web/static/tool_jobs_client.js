/* Polling state for one tool job. Rendering and authenticated transport are supplied by the caller.
 *
 * create({ request, onChange?, pollMs?, timeoutMs?, maxFailures?, setTimeout?, clearTimeout? })
 * request(method, path, body, signal) resolves { status, body } with parsed JSON and real HTTP status.
 * The transport supplies same-origin credentials and the current CSRF header, honors AbortSignal,
 * bounds response bodies, and never retries a mutation. Render strings through textContent.
 *
 * start(pack) posts once; watch(jobId) observes an existing job without starting or cancelling it.
 * refresh() retries observation; cancel() sends at most one request per watched job. dispose() stops
 * this observer, not the server job. Persist the validated jobId in the UI before disposal if needed.
 * Methods resolve true when handled, false when disallowed/stale. Invalid local input rejects.
 *
 * getState()/onChange receive detached data. observation describes connection/client state;
 * snapshot.state, when present, is the last observed server outcome. resultStatus is independent.
 * Unknown start outcome disables start: reconcile it explicitly instead of submitting it again.
 */
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.CCToolJobs = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const states = new Set(["queued", "running", "succeeded", "failed", "cancelled"]);
  const terminal = new Set(["succeeded", "failed", "cancelled"]);
  const jobId = value => typeof value === "string" && /^[a-f0-9]{32}$/.test(value);
  const record = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const text = (value, limit) => typeof value === "string" && value.length <= limit * 2 &&
    Array.from(value).length <= limit;
  const counter = value => value === null || (Number.isSafeInteger(value) && value >= 0);
  const detached = value => JSON.parse(JSON.stringify(value));

  function snapshot(body, id) {
    if (!record(body) || body.job_id !== id || !text(body.tool, 200) || !states.has(body.state) ||
        !text(body.phase, 200) || !counter(body.completed) || !counter(body.total) ||
        !text(body.error, 2012) || !Array.isArray(body.log) ||
        body.active !== !terminal.has(body.state)) return null;
    const log = body.log.slice(-200);
    if (!log.every(line => text(line, 2012))) return null;
    return { job_id: id, tool: body.tool, state: body.state, phase: body.phase,
      completed: body.completed, total: body.total, error: body.error, log, active: body.active };
  }

  function result(body, tool) {
    if (!record(body) || body.state !== "succeeded") return null;
    if (body.result_status === "unavailable" && body.error_code === "result_metadata_unavailable") {
      return { state: "succeeded", result_status: "unavailable",
        error_code: "result_metadata_unavailable" };
    }
    if (body.schema_version !== 1 || body.tool !== tool || body.source !== "bundled" ||
        body.verification_method !== "sha256" || !text(body.path, 2048) || !body.path ||
        !text(body.version, 2048)) return null;
    return { schema_version: 1, tool, path: body.path, version: body.version, source: "bundled",
      verification_method: "sha256", state: "succeeded" };
  }

  function create(options) {
    if (!options || typeof options.request !== "function") throw new TypeError("request is required");
    const request = options.request;
    const onChange = options.onChange || function () {};
    const later = options.setTimeout || setTimeout;
    const cancelTimer = options.clearTimeout || clearTimeout;
    const interval = options.pollMs === undefined ? 1000 : options.pollMs;
    const maxFailures = options.maxFailures === undefined ? 5 : options.maxFailures;
    const timeout = options.timeoutMs === undefined ? 15000 : options.timeoutMs;
    if (typeof onChange !== "function" || !Number.isInteger(interval) || interval < 100 ||
        interval > 60000 || !Number.isInteger(maxFailures) || maxFailures < 1 || maxFailures > 10 ||
        !Number.isInteger(timeout) || timeout < 100 || timeout > 120000) {
      throw new TypeError("invalid polling options");
    }
    let epoch = 0, timer = null, pollFlight = null, cancelFlight = null, failures = 0;
    let abort = new AbortController();
    let disposed = false;
    let data = initial();

    function initial() {
      return { observation: "idle", jobId: null, snapshot: null, result: null,
        resultStatus: "none", cancel: "none", notice: null };
    }
    function current(token) { return !disposed && token === epoch; }
    function view() {
      return detached(Object.assign({}, data, { canStart: !disposed && pollFlight === null &&
        (data.observation === "idle" || data.observation === "rejected" ||
          (data.snapshot !== null && terminal.has(data.snapshot.state))) }));
    }
    function publish() {
      // An observer must not change a completed request into a transport failure.
      try { onChange(view()); } catch (_) { /* Rendering failure does not change server state. */ }
    }
    function clearTimer() {
      if (timer !== null) cancelTimer(timer);
      timer = null;
    }
    function replace() {
      epoch += 1;
      abort.abort();
      abort = new AbortController();
      clearTimer();
      pollFlight = null;
      cancelFlight = null;
      failures = 0;
      data = initial();
      return epoch;
    }
    async function send(token, method, path, body) {
      if (!current(token)) return null;
      const lifetime = abort.signal;
      const operation = new AbortController();
      return new Promise(function (resolve, reject) {
        let finished = false;
        let deadline = null;
        function finish(ok, value) {
          if (finished) return;
          finished = true;
          if (deadline !== null) cancelTimer(deadline);
          lifetime.removeEventListener("abort", stopped);
          if (ok) resolve(value); else reject(value);
        }
        function stopped() {
          operation.abort();
          finish(false, new Error("Observation stopped"));
        }
        lifetime.addEventListener("abort", stopped, { once: true });
        deadline = later(function () {
          operation.abort();
          finish(false, new Error("Request timed out"));
        }, timeout);
        Promise.resolve().then(function () {
          if (!finished) return request(method, path, body, operation.signal);
        }).then(value => finish(true, value), error => finish(false, error));
      });
    }
    function pause(notice) {
      data.observation = "paused";
      data.notice = notice;
    }
    function schedule(token, delay) {
      if (!current(token) || timer !== null) return;
      timer = later(function () {
        timer = null;
        if (current(token)) void poll(token);
      }, delay);
    }
    function statusFailure() {
      failures += 1;
      if (failures >= maxFailures) pause("status_unavailable");
      else {
        data.observation = "reconnecting";
        data.notice = "status_unavailable";
      }
    }

    async function readResult(token) {
      data.resultStatus = "loading";
      publish();
      if (!current(token)) return;
      let response;
      try { response = await send(token, "GET", "/api/crack/job/" + data.jobId + "/result"); }
      catch (_) { response = null; }
      if (!current(token)) return;
      const parsed = response && response.status === 200 ? result(response.body, data.snapshot.tool) : null;
      data.result = parsed;
      data.resultStatus = parsed && !parsed.result_status ? "available" : "unavailable";
      data.notice = data.resultStatus === "unavailable" ? "result_metadata_unavailable" : null;
      // An observed success survives pruning, authentication loss and malformed metadata.
      data.observation = "complete";
    }

    async function poll(token) {
      if (!current(token) || !data.jobId || pollFlight === token) return false;
      clearTimer();
      pollFlight = token;
      try {
        if (data.snapshot && data.snapshot.state === "succeeded") {
          await readResult(token);
        } else {
          let response;
          try { response = await send(token, "GET", "/api/crack/job/" + data.jobId); }
          catch (_) { response = null; }
          if (!current(token)) return false;
          if (!response || response.status >= 500) statusFailure();
          else if (response.status === 401 || response.status === 403) pause("authentication_required");
          else if (response.status === 404) pause("job_unavailable");
          else {
            const parsed = response.status === 200 ? snapshot(response.body, data.jobId) : null;
            if (!parsed || (data.snapshot && (parsed.tool !== data.snapshot.tool ||
                (data.snapshot.state === "running" && parsed.state === "queued")))) {
              pause("invalid_status");
            }
            else {
              failures = 0;
              data.snapshot = parsed;
              data.notice = null;
              data.observation = parsed.active ? "tracking" : "complete";
              if (!parsed.active && data.cancel !== "none") data.cancel = "closed";
              if (parsed.state === "succeeded") await readResult(token);
            }
          }
        }
      } finally {
        if (current(token)) {
          pollFlight = null;
          publish();
          if (current(token) && (data.observation === "tracking" || data.observation === "reconnecting")) {
            schedule(token, Math.min(30000, interval * Math.pow(2, failures)));
          }
        }
      }
      return current(token);
    }

    async function start(pack) {
      if (!text(pack, 256) || !pack || pack.includes("\0")) throw new TypeError("invalid pack name");
      if (!view().canStart) return false;
      const token = replace();
      data.observation = "starting";
      publish();
      if (!current(token)) return false;
      let response;
      try { response = await send(token, "POST", "/api/crack/enable-bundled/async", { pack }); }
      catch (_) { response = null; }
      if (!current(token)) return false;
      if (response && response.status === 202 && record(response.body) && jobId(response.body.job_id)) {
        data.jobId = response.body.job_id;
        data.observation = "tracking";
        publish();
        if (current(token)) await poll(token);
      } else {
        const rejected = response && [400, 401, 403, 409, 422, 503].includes(response.status);
        data.observation = rejected ? "rejected" : "unknown";
        const notices = { 400: "invalid_request", 401: "authentication_required",
          403: "authentication_required", 409: "destination_busy", 422: "unsupported",
          503: "worker_unavailable" };
        data.notice = rejected ? notices[response.status] : "start_outcome_unknown";
        publish();
      }
      return current(token);
    }

    async function watch(id) {
      if (!jobId(id)) throw new TypeError("invalid job id");
      if (disposed || data.observation === "starting") return false;
      const token = replace();
      data.jobId = id;
      data.observation = "tracking";
      publish();
      return current(token) ? poll(token) : false;
    }

    async function cancel() {
      const token = epoch;
      if (!current(token) || !data.jobId || cancelFlight === token ||
          !data.snapshot || !data.snapshot.active || data.cancel !== "none") return false;
      cancelFlight = token;
      data.cancel = "sending";
      publish();
      let response;
      try { response = await send(token, "POST", "/api/crack/job/" + data.jobId + "/cancel", {}); }
      catch (_) { response = null; }
      if (!current(token)) return false;
      cancelFlight = null;
      // A late cancel response cannot overwrite a terminal outcome from a concurrent poll.
      if (data.snapshot && terminal.has(data.snapshot.state)) return true;
      if (response && response.status === 200 && record(response.body) &&
          typeof response.body.cancel_requested === "boolean") {
        data.cancel = response.body.cancel_requested ? "requested" : "too_late";
      } else data.cancel = "unknown";
      publish();
      return current(token);
    }

    function dispose() {
      if (disposed) return;
      replace();
      disposed = true;
      data.observation = "disposed";
      publish();
    }

    return Object.freeze({ start, watch, cancel, getState: view,
      refresh: function () {
        if (disposed || !data.jobId) return Promise.resolve(false);
        if (data.snapshot && ["failed", "cancelled"].includes(data.snapshot.state)) {
          return Promise.resolve(false);
        }
        failures = 0;
        return poll(epoch);
      }, dispose });
  }

  return Object.freeze({ create });
});
