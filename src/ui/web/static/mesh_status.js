/* Read-only Meshtastic status workspace (DEVICE ▸ Mesh, first card).
 *
 * Reads GET /api/mesh/status (same-origin, CSRF) and renders the OWNED node's honest status: identity,
 * battery/external-power, link SNR, and how long ago the node was last heard — with distinct loading / ready /
 * unavailable / failed states. The API exposes telemetry only for an admitted, config-ready node on a healthy
 * transport; every other case is an explicit unavailable observation (never proof of physical disconnection).
 * This view sends nothing, owns no transport, and shows only what the API allowlists.
 *
 * A generation counter guards overlapping refreshes AND view changes: only the newest request paints. Leaving
 * the Mesh view (main-nav OR subtab) invalidates any in-flight read so a late response can't paint while hidden;
 * returning with Mesh shown requests a fresh snapshot.
 */
(function () {
  "use strict";

  var generation = 0;
  var wasVisible = false;

  function el(id) { return document.getElementById(id); }

  function meshVisible() {
    // The Mesh workspace is visible only when the on-view's on-sub is the mesh sub.
    return !!(document.querySelector && document.querySelector('.view.on .sub.on[data-sub="mesh"]'));
  }

  function fetchStatus() {
    return fetch("/api/mesh/status", {
      headers: { "X-CSRF-Token": window.CSRF_TOKEN || "" }, credentials: "same-origin",
    }).then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); });
  }

  // Observation-availability wording — never asserts physical connectivity for an unwired/failed provider.
  var REASONS = {
    "provider-absent": "No mesh observation reader is available.",
    "provider-error": "The mesh observation reader is unavailable right now.",
    "owner-not-admitted": "No owned mesh node is admitted for reading yet.",
    "inventory-not-ready": "The mesh node's inventory is not ready (syncing or resync required).",
    "transport-uncertain": "The mesh transport is uncertain; no current observation.",
    "transport-retired": "The mesh session was retired; no current observation.",
  };

  function batteryLabel(b) {
    if (!b || b.state === "unknown") return "Battery: unknown";
    if (b.state === "external") return "Power: external" + (b.voltage != null ? " (" + b.voltage + " V)" : "");
    return "Battery: " + b.percent + "%" + (b.voltage != null ? " (" + b.voltage + " V)" : "");
  }

  function linkLabel(l) {
    if (!l || l.state !== "known" || l.snr_db == null) return "Link SNR: unknown";
    return "Link SNR: " + l.snr_db + " dB";
  }

  function freshnessLabel(f) {
    if (!f) return "Last heard: unknown";
    if (f.clock === "ambiguous") return "Last heard: time uncertain (node clock)";
    if (f.last_heard_age_s === "unknown" || f.last_heard_epoch == null) return "Last heard: unknown";
    return "Last heard: " + f.last_heard_age_s + " s ago";
  }

  /* Leak-free display model. Unavailable when the API withholds telemetry; otherwise the owned node's status.
   * Battery/link measurement age is not implied — only last-heard age, and only when the node clock is sound. */
  function viewModel(s) {
    if (!s || s.available === false) {
      var reason = (s && s.reason) || "unknown";
      return { state: "unavailable", reason: reason,
               message: REASONS[reason] || "Mesh status is unavailable." };
    }
    return {
      state: "ready",
      num: s.identity.num,
      name: s.identity.name,
      battery: batteryLabel(s.battery),
      link: linkLabel(s.link),
      freshness: freshnessLabel(s.freshness),
      transport: (s.readiness && s.readiness.transport) || "unknown",
    };
  }

  function line(text) {
    var d = document.createElement("div");
    d.className = "card2";
    d.textContent = text;
    return d;
  }

  function paint(vm) {
    var stateEl = el("mesh-status-state");
    if (stateEl) stateEl.textContent = vm.state;
    var body = el("mesh-status-body");
    if (!body) return;
    while (body.firstChild) body.removeChild(body.firstChild);
    if (vm.state === "loading") { body.appendChild(line("Reading mesh status…")); return; }
    if (vm.state === "unavailable") { body.appendChild(line(vm.message)); return; }
    if (vm.state === "failed") { body.appendChild(line("Could not read mesh status — try Refresh.")); return; }
    body.appendChild(line(vm.name + "  (node " + vm.num + ")"));
    body.appendChild(line(vm.battery));
    body.appendChild(line(vm.link));
    body.appendChild(line(vm.freshness));
    body.appendChild(line("Battery/link measurement age: unknown (no per-metric timestamp)."));
  }

  function refresh() {
    var mine = ++generation;
    if (el("mesh-status-body")) paint({ state: "loading" });
    return fetchStatus().then(
      function (s) { if (mine === generation) paint(viewModel(s)); },
      function () { if (mine === generation) paint({ state: "failed" }); }
    );
  }

  /* Called by reform.js after any navigation (main-view OR subtab). Refreshes on becoming visible; on becoming
   * hidden it bumps the generation so an in-flight response is dropped and cannot paint the hidden view. */
  function syncVisibility() {
    var vis = meshVisible();
    if (vis && !wasVisible) { wasVisible = true; refresh(); }
    else if (!vis && wasVisible) { wasVisible = false; generation++; }
  }

  window.CCMeshStatus = { refresh: refresh, syncVisibility: syncVisibility, viewModel: viewModel };

  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("DOMContentLoaded", function () {
      var btn = el("mesh-status-refresh");
      if (btn) btn.addEventListener("click", refresh);
      syncVisibility();   // read now if the Mesh view is the one shown on load
    });
  }
})();
