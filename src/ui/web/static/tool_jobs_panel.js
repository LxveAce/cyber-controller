/* Optional-tool job controls. Local tracking is a recovery hint, never authorization. */
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) module.exports = factory(require("./tool_jobs_client.js"));
  else root.CCToolJobPanel = factory(root.CCToolJobs);
})(typeof globalThis !== "undefined" ? globalThis : this, function (jobs) {
  "use strict";
  const KEY = "cc.tool-job.v1";
  const LIMIT = 1024 * 1024;
  const idOK = value => typeof value === "string" && /^[a-f0-9]{32}$/.test(value);
  const nameOK = value => typeof value === "string" && value.length > 0 && value.length <= 256 && !value.includes("\0");
  const record = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const terminal = state => ["succeeded", "failed", "cancelled"].includes(state);

  async function transport(method, path, body, signal) {
    const response = await fetch(path, {
      method, signal, credentials: "same-origin", redirect: "error",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": window.CSRF_TOKEN || "" },
      body: method === "GET" ? undefined : JSON.stringify(body || {}),
    });
    let parsed = null;
    try {
      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8", { fatal: true });
      let bytes = 0, text = "";
      try {
        for (;;) {
          const chunk = await reader.read();
          if (chunk.done) break;
          bytes += chunk.value.byteLength;
          if (bytes > LIMIT) throw new Error("Response is too large");
          text += decoder.decode(chunk.value, { stream: true });
        }
        parsed = JSON.parse(text + decoder.decode());
      } catch (_) {
        // Cancelling observation must not wait for a remote body to finish.
        void reader.cancel().catch(function () {});
      } finally { reader.releaseLock(); }
    } catch (_) { /* Preserve HTTP status even when its body is unreadable. */ }
    return { status: response.status, body: parsed };
  }

  function create(options) {
    const request = options.request || transport;
    const notify = options.onChange || function () {};
    const refreshed = options.onComplete || function () {};
    const storage = options.storage;
    let generation = 0, client = null, marker = null, locked = false;
    let trustedStart = false, disposed = false, completed = false;
    let state = { mode: "idle", name: "", job: null, message: "", storageNote: "" };

    function view() { return JSON.parse(JSON.stringify(Object.assign({}, state, {
      locked, cancellationRecorded: Boolean(marker && marker.cancelSent),
    }))); }
    function publish() { try { notify(view()); } catch (_) { /* Keep ownership if rendering fails. */ } }
    function writeMarker() {
      try { storage.setItem(KEY, JSON.stringify(marker)); state.storageNote = ""; return true; }
      catch (_) {
        state.storageNote = "Local tracking could not be saved. Keep this page open; a reload may not recover the job.";
        return false;
      }
    }
    function removeMarker() {
      try { storage.removeItem(KEY); marker = null; state.storageNote = ""; return true; }
      catch (_) {
        state.storageNote = "Local tracking could not be cleared. New installs remain disabled until it can be cleared.";
        return false;
      }
    }
    function release() { locked = !removeMarker(); }
    function makeClient(token, source, name) {
      const startPath = jobs.startRequest(source, name).path;
      return jobs.create({ source, request: async function (method, path, body, signal) {
        const response = await request(method, path, body, signal);
        if (token === generation && method === "POST" && path === startPath) {
          // Status alone does not prove that an unreadable POST response rejected the job.
          trustedStart = record(response.body) &&
            ((response.status === 202 && idOK(response.body.job_id)) ||
             ([400, 401, 403, 409, 422, 503].includes(response.status) &&
              typeof response.body.error === "string" && response.body.error.length > 0));
        }
        return response;
      }, onChange: function (next) {
        if (disposed || token !== generation) return;
        state.job = next;
        if (next.jobId && marker && marker.jobId !== next.jobId) {
          marker.jobId = next.jobId;
          writeMarker();
        }
        if (next.observation === "rejected") {
          if (trustedStart) { state.mode = "rejected"; release(); }
          else state.mode = "unresolved";
        } else if (next.observation === "unknown") state.mode = "unresolved";
        else if (next.snapshot && terminal(next.snapshot.state)) {
          state.mode = "terminal";
          if (!completed) {
            completed = true;
            release();
            try { refreshed(); } catch (_) { /* Detection is separate from the job result. */ }
          }
        } else if (next.observation !== "idle" && next.observation !== "disposed") state.mode = "tracking";
        publish();
      }});
    }
    function reserve(kind, name) {
      if (disposed || locked || !nameOK(name)) return false;
      locked = true; // Synchronous: a second button cannot replace the pending marker.
      generation += 1;
      const reservedGeneration = generation;
      if (client) client.dispose();
      client = null;
      completed = false;
      trustedStart = false;
      marker = { version: 1, kind, name, jobId: null, cancelSent: false };
      state = { mode: kind === "legacy" ? "legacy" : "tracking", name, job: null, message: "", storageNote: "" };
      if (!writeMarker()) {
        state.mode = "unresolved";
        state.message = "The install was not started because local tracking is unavailable.";
        publish();
        return false;
      }
      publish();
      return !disposed && reservedGeneration === generation;
    }
    async function start(name) {
      if (!reserve("bundled", name)) return false;
      const token = generation;
      client = makeClient(token, "bundled", name);
      return client.start(name);
    }
    async function startDownload(name) {
      if (!reserve("download", name)) return false;
      const token = generation;
      client = makeClient(token, "download", name);
      return client.start(name);
    }
    async function runLegacy(name, action) {
      if (!reserve("legacy", name)) return false;
      const token = generation;
      try {
        await action();
        if (disposed || token !== generation) return false;
        state.mode = "legacy-complete";
        state.message = "The download request completed. Re-check the detected tool before use.";
        release();
        try { refreshed(); } catch (_) { /* Preserve the observed request result. */ }
      } catch (_) {
        if (disposed || token !== generation) return false;
        state.mode = "unresolved";
        state.message = "The download request could not be confirmed. It has not been retried.";
      }
      publish();
      return true;
    }
    function forget() {
      if (disposed) return false;
      generation += 1; // Retire every old callback before removing local ownership.
      if (client) client.dispose();
      client = null;
      if (!removeMarker()) { locked = true; state.mode = "unresolved"; publish(); return false; }
      locked = false;
      state = { mode: "idle", name: "", job: null, message: "Local tracking forgotten. No server operation was stopped or restarted.", storageNote: "" };
      publish();
      return true;
    }
    async function cancel() {
      if (!client || !marker || marker.cancelSent || !state.job ||
          !state.job.snapshot || !state.job.snapshot.active) return false;
      marker.cancelSent = true;
      if (!writeMarker()) {
        marker.cancelSent = false; // No POST was dispatched, so a later manual retry remains valid.
        state.storageNote = "Cancellation was not sent because its tracking could not be saved. The job may still be running.";
        publish(); return false;
      }
      publish();
      return client.cancel();
    }
    function restore() {
      let raw;
      try { raw = storage.getItem(KEY); }
      catch (_) { locked = true; state.mode = "unresolved"; state.message = "Local tracking could not be read. A previous install may still be running."; publish(); return; }
      if (raw === null) { publish(); return; }
      try {
        if (typeof raw !== "string" || raw.length > 1024) throw new Error("Invalid tracking record");
        const saved = JSON.parse(raw);
        if (!record(saved) || saved.version !== 1 || !["bundled", "download", "legacy"].includes(saved.kind) ||
            !nameOK(saved.name) || (saved.jobId !== null && !idOK(saved.jobId)) ||
            typeof saved.cancelSent !== "boolean" || (saved.kind === "legacy" && saved.jobId !== null)) {
          throw new Error("Invalid tracking record");
        }
        marker = { version: 1, kind: saved.kind, name: saved.name, jobId: saved.jobId, cancelSent: saved.cancelSent };
      } catch (_) { locked = true; state.mode = "unresolved"; state.message = "Local tracking is invalid. A previous install may still be running."; publish(); return; }
      locked = true;
      state.name = marker.name;
      if (marker.jobId) {
        state.mode = "tracking";
        client = makeClient(++generation, marker.kind, marker.name);
        void client.watch(marker.jobId);
      } else {
        state.mode = "unresolved";
        state.message = "The previous request has no recoverable job ID. Its outcome is unknown; it has not been repeated.";
        publish();
      }
    }
    const api = { start, startDownload, runLegacy, forget, cancel, getState: view,
      refresh: function () { return client ? client.refresh() : Promise.resolve(false); },
      canCancel: function () { return Boolean(client && marker && !marker.cancelSent && state.job &&
        state.job.snapshot && state.job.snapshot.active && state.job.cancel === "none"); },
      dispose: function () { if (disposed) return; disposed = true; generation += 1; if (client) client.dispose(); },
    };
    restore();
    return Object.freeze(api);
  }

  function mount(options) {
    const host = options.element, inventory = options.inventory;
    if (!host || !inventory || !jobs) return null;
    let controller;
    host.innerHTML = '<h3><span class="t">Tool installation</span></h3>' +
      '<div class="tool-job-status" role="status" aria-live="polite"></div>' +
      '<progress class="tool-job-progress" aria-label="Installation progress"></progress>' +
      '<div class="footnote tool-job-note"></div><div class="footnote tool-job-storage"></div>' +
      '<pre class="term tool-job-log" aria-label="Installation log" hidden></pre>' +
      '<div class="row tool-job-actions"><button class="btn sm tool-job-refresh">Refresh status</button>' +
      '<button class="btn sm tool-job-cancel">Request cancel</button></div>' +
      '<details class="tool-job-forget"><summary>Tracking recovery</summary>' +
      '<p class="footnote">Forgetting removes this tab\'s tracking only. The server job may still be running. It does not cancel, restart or repeat an install.</p>' +
      '<button class="btn sm tool-job-forget-button">Forget local tracking</button></details>';
    const select = cls => host.querySelector(".tool-job-" + cls);
    const notices = {
      authentication_required: "Authentication is needed to read this job. Its outcome is unknown.",
      job_unavailable: "This job is unavailable or no longer belongs to this session. Its outcome is unknown.",
      status_unavailable: "Status is unavailable. The server job may still be running.",
      invalid_status: "The server returned an unreadable job status. Its outcome is unknown.",
      destination_busy: "Another installation already owns this tool's destination.",
      invalid_request: "The server rejected this request.", unsupported: "This operation is not available for this tool.",
      worker_unavailable: "The server could not start the installation.",
      start_outcome_unknown: "The start request could not be confirmed. It has not been retried.",
      result_metadata_unavailable: "Installation succeeded, but its result details could not be read.",
    };
    function render(state) {
      host.hidden = state.mode === "idle" && !state.message;
      const job = state.job, snap = job && job.snapshot;
      let status = state.message || "Preparing the install request…";
      if (state.mode === "unresolved") status = state.message || "The request outcome is unknown. It has not been retried.";
      else if (state.mode === "legacy") status = "Downloading " + state.name + "… Progress and cancellation are unavailable for this download path.";
      else if (state.mode === "rejected") status = notices[job.notice] || "The server rejected the request.";
      else if (snap) status = state.name + " · " + snap.state + (snap.phase ? " · " + snap.phase : "");
      else if (state.mode === "tracking") status = state.name + " · waiting for job status…";
      const statusNode = select("status");
      if (statusNode.textContent !== status) statusNode.textContent = status;
      const progress = select("progress");
      progress.hidden = !snap || !snap.active;
      if (snap && Number.isSafeInteger(snap.completed) && Number.isSafeInteger(snap.total) &&
          snap.total > 0 && snap.completed >= 0 && snap.completed <= snap.total) {
        progress.max = snap.total; progress.value = snap.completed;
      } else progress.removeAttribute("value");
      let note = job && job.notice ? (notices[job.notice] || "Status could not be confirmed.") : "";
      if (state.mode === "unresolved" && job && job.observation === "rejected") {
        note = "The response could not be read as a valid rejection. Check the tool's current status before forgetting local tracking.";
      }
      if (snap && snap.error) note = snap.error;
      if (job && job.resultStatus === "available" && job.result && job.result.schema_version === 1) {
        const methods = { sha256: "SHA-256 checked.", sha1: "SHA-1 checked.", size: "Size checked; no hash verification." };
        note += " " + methods[job.result.verification_method];
      }
      if (job && job.cancel === "requested") note += " Cancellation requested; waiting for the server outcome.";
      if (job && job.cancel === "sending") note += " Sending the cancellation request…";
      if (job && job.cancel === "too_late") note += " Cancellation is too late; the operation is finishing.";
      if (job && job.cancel === "unknown") note += " The cancellation request could not be confirmed. It has not been repeated.";
      if (state.cancellationRecorded && job && job.cancel === "none") {
        note += " Cancellation may already have been requested in this tab; it will not be sent again.";
      }
      select("note").textContent = note;
      select("storage").textContent = state.storageNote;
      const log = select("log");
      log.hidden = !snap || !snap.log.length;
      log.textContent = snap ? snap.log.join("\n") : "";
      select("refresh").disabled = !job || !job.jobId;
      select("cancel").disabled = !controller || !controller.canCancel();
      select("forget").hidden = !state.locked;
      inventory.querySelectorAll("button[data-enable],button[data-install]").forEach(button => { button.disabled = state.locked; });
    }
    let storage;
    try { storage = window.sessionStorage; } catch (_) { storage = null; }
    controller = create({ storage, onChange: render, onComplete: options.onComplete });
    select("refresh").addEventListener("click", function () { void controller.refresh(); });
    select("cancel").addEventListener("click", function () { void controller.cancel(); });
    select("forget-button").addEventListener("click", function () { controller.forget(); });
    render(controller.getState());
    return Object.freeze({ start: controller.start, startDownload: controller.startDownload, runLegacy: controller.runLegacy,
      refreshControls: function () { render(controller.getState()); } });
  }
  return Object.freeze({ create, mount, transport, storageKey: KEY });
});
