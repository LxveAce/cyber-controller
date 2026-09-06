/* Request ownership and response validation for the manual Updates card. */
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.CCUpdatesCard = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const RELEASES = "https://github.com/LxveAce/cyber-controller/releases";
  const record = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const version = value => typeof value === "string" && value.length <= 120 && value.trim() === value &&
    /^[vV]?[0-9][A-Za-z0-9._+-]*$/.test(value);
  const tag = value => typeof value === "string" && value.length <= 120 && value.trim() === value &&
    /^[A-Za-z0-9][A-Za-z0-9._+-]*$/.test(value);
  const has = (value, key) => Object.prototype.hasOwnProperty.call(value, key);

  function releaseURL(value) {
    return typeof value === "string" && value.length <= 400 && value.trim() === value &&
      /^https:\/\/github\.com\/LxveAce\/cyber-controller\/releases(?:\/tag\/[A-Za-z0-9][A-Za-z0-9._+-]{0,119})?$/.test(value)
      ? value : RELEASES;
  }

  function parseReply(value, cachedVersion) {
    if (!record(value) || typeof value.ok !== "boolean") throw new Error("Invalid update reply");
    const current = has(value, "current") ? value.current : null;
    if (has(value, "current") && !version(current)) throw new Error("Invalid current version");
    if (value.status === "OFFLINE") {
      if ((has(value, "behind") && value.behind !== 0) ||
          (has(value, "latest_tag") && value.latest_tag !== "") ||
          (has(value, "latest_url") && value.latest_url !== "")) throw new Error("Invalid offline reply");
      return { kind: "offline", current: current || (version(cachedVersion) ? cachedVersion : null) };
    }
    if (!value.ok || !version(current) || !Number.isSafeInteger(value.behind)) throw new Error("Invalid update result");
    if (value.status === "UP_TO_DATE" && value.behind === 0 &&
        (value.latest_tag === "" || tag(value.latest_tag))) return { kind: "up_to_date", current };
    if (value.status === "NEWER" && value.behind > 0 && tag(value.latest_tag)) {
      return { kind: "newer", current, latestTag: value.latest_tag, latestURL: releaseURL(value.latest_url) };
    }
    throw new Error("Unknown update result");
  }

  function create(options) {
    const getVersion = options.getVersion, request = options.check, observer = options.onState;
    const now = options.now || (() => performance.now());
    const later = options.setTimeout || setTimeout, cancelTimer = options.clearTimeout || clearTimeout;
    const timeout = options.timeoutMs === undefined ? 30000 : options.timeoutMs;
    if (typeof getVersion !== "function" || typeof request !== "function" || typeof observer !== "function" ||
        !Number.isInteger(timeout) || timeout < 100 || timeout > 120000) throw new Error("Invalid Updates card options");
    let cached = null, epoch = 0, manualID = 0, suspended = false, manualStarted = false, initialStarted = false;
    let initial = null, active = null, initialPromise = null;
    let state = Object.freeze({ kind: "version_loading", current: null, busy: false });

    function publish(next) {
      state = Object.freeze(next);
      if (!suspended) {
        try { observer(state); } catch (_) { /* A renderer cannot retain a request slot. */ }
      }
    }
    function owns(op) { return !op.done && !suspended && op.epoch === epoch && (op.manual ? active : initial) === op; }
    function currentGeneration(op) { return !suspended && op.epoch === epoch && (!op.manual || op.id === manualID); }
    function clear(op, abort) {
      const timer = op.timer;
      op.timer = null;
      try { if (timer !== null) cancelTimer(timer); } catch (_) { /* Continue retiring this owner. */ }
      try { if (abort) op.controller.abort(); } catch (_) { /* Late callbacks remain fenced. */ }
    }
    function finish(op, ok, data, abort) {
      if (!owns(op)) return;
      let next = null, found = null;
      if (now() >= op.deadline) { ok = false; abort = true; }
      try {
        if (ok) {
          if (op.manual) { next = parseReply(data, cached); found = next.current; }
          else if (record(data) && version(data.version)) found = data.version;
          else ok = false;
        }
      } catch (_) { ok = false; }
      if (!owns(op)) return;
      if (now() >= op.deadline) { ok = false; abort = true; }
      op.done = true;
      if (op.manual) active = null;
      else initial = null;
      // Retire before callbacks: timer/abort observers can synchronously start a retry.
      clear(op, abort);
      if (currentGeneration(op)) {
        if (ok && found && (op.manual || cached === null)) cached = found;
        if (op.manual) publish(ok ? { ...next, busy: false } : { kind: "error", current: cached, busy: false });
        else if (!manualStarted) publish({ kind: ok ? "version" : "version_unavailable", current: cached, busy: false });
      }
      op.resolve(ok && currentGeneration(op));
    }
    function launch(manual) {
      const op = { manual, id: manual ? ++manualID : 0, epoch, done: false, timer: null,
        controller: new AbortController(), deadline: now() + timeout, promise: null, resolve: null };
      op.promise = new Promise(resolve => { op.resolve = resolve; });
      if (manual) { active = op; manualStarted = true; }
      else initial = op;
      if (manual) publish({ kind: "checking", current: cached, busy: true });
      if (!owns(op)) return op.promise;
      const timer = later(() => finish(op, false, null, true), Math.max(0, op.deadline - now()));
      if (owns(op)) op.timer = timer;
      else cancelTimer(timer);
      Promise.resolve().then(function () {
        if (owns(op) && now() >= op.deadline) { finish(op, false, null, true); return; }
        if (owns(op)) return (manual ? request : getVersion)(op.controller.signal);
      }).then(value => finish(op, true, value, false), () => finish(op, false, null, false));
      return op.promise;
    }
    return {
      start: function () {
        if (suspended) return Promise.resolve(false);
        if (initialStarted) return initialPromise || (initial ? initial.promise : Promise.resolve(false));
        initialStarted = true;
        initialPromise = launch(false);
        return initialPromise;
      },
      check: function () {
        if (suspended) return Promise.resolve(false);
        return active ? active.promise : launch(true);
      },
      suspend: function () {
        if (suspended) return;
        suspended = true; epoch++;
        const old = [initial, active];
        initial = active = null;
        if (state.kind === "checking") state = Object.freeze({ kind: "cancelled", current: cached, busy: false });
        else if (state.kind === "version_loading") state = Object.freeze({ kind: "version_unavailable", current: cached, busy: false });
        // Detach both owners before an abort listener can resume and start another operation.
        old.forEach(op => { if (op) op.done = true; });
        old.forEach(op => { if (op) { clear(op, true); op.resolve(false); } });
      },
      resume: function () {
        if (!suspended) return;
        suspended = false;
        publish({ ...state });
      },
    };
  }
  return { create, parseReply, releaseURL, RELEASES };
});
