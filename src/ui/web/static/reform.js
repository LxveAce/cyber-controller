/* Cyber Controller — reform web shell behavior.
 *
 * Two jobs:
 *   1) the mockup's own navigation (rail views, sub-tabs, depth toggle, terminal picker) — verbatim logic
 *      from the mockup so the shell feels identical;
 *   2) LIVE hydration of DEVICE ▸ Dashboard from the real Flask/core endpoints (/api/system-health,
 *      /api/devices, /api/targets) + the authenticated Socket.IO serial stream. Everything else is the
 *      static design preview that Phase 2 wires up the same way.
 *
 * Served same-origin ('self') and tagged with the per-request CSP nonce, so the strict CSP needs no
 * relaxation.
 */
(function () {
  "use strict";

  // ── mockup navigation ─────────────────────────────────────────────
  var crumbNames = { device: "DEVICE", hunt: "HUNT", operate: "OPERATE", crack: "CRACK", map: "MAP", terminal: "TERMINAL", settings: "SETTINGS" };
  var crumb = document.getElementById("crumb");

  document.getElementById("rail").addEventListener("click", function (e) {
    var it = e.target.closest(".navitem");
    if (!it) return;
    var v = it.dataset.view;
    document.querySelectorAll(".rail .navitem").forEach(function (n) { n.classList.toggle("on", n === it); });
    document.querySelectorAll(".main .view").forEach(function (sec) { sec.classList.toggle("on", sec.dataset.view === v); });
    var view = document.querySelector('.view[data-view="' + v + '"]');
    var tabs = view.querySelector(".subtabs button.on");
    var subName = tabs ? tabs.textContent : "";
    crumb.innerHTML = "<b>" + crumbNames[v] + "</b>" + (subName ? " ▸ " + subName : "");
    document.getElementById("main").scrollTop = 0;
  });

  document.querySelectorAll(".subtabs").forEach(function (bar) {
    bar.addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (!b) return;
      var scope = bar.parentElement;
      bar.querySelectorAll("button").forEach(function (x) { x.classList.toggle("on", x === b); });
      scope.querySelectorAll(":scope > .sub").forEach(function (s) { s.classList.toggle("on", s.dataset.sub === b.dataset.sub); });
      if (bar.dataset.tabs && crumbNames[scope.dataset.view]) {
        crumb.innerHTML = "<b>" + crumbNames[scope.dataset.view] + "</b> ▸ " + b.textContent;
      }
    });
  });

  var opPills = document.getElementById("op-pills");
  if (opPills) {
    opPills.addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (!b) return;
      document.querySelectorAll("#op-pills button").forEach(function (x) { x.classList.toggle("on", x === b); });
      document.querySelectorAll(".opane").forEach(function (p) { p.classList.toggle("on", p.dataset.opane === b.dataset.opane); });
    });
  }

  var termlist = document.getElementById("termlist");
  if (termlist) {
    termlist.addEventListener("click", function (e) {
      var r = e.target.closest(".termrow");
      if (!r) return;
      document.querySelectorAll("#termlist .termrow").forEach(function (x) { x.classList.toggle("on", x === r); });
      document.querySelectorAll(".termpane").forEach(function (p) { p.classList.toggle("on", p.dataset.term === r.dataset.term); });
    });
  }

  document.getElementById("depth").addEventListener("click", function (e) {
    var b = e.target.closest("button");
    if (!b) return;
    document.querySelectorAll("#depth button").forEach(function (x) { x.classList.toggle("on", x === b); });
    document.getElementById("app").classList.toggle("pro-hidden", b.dataset.depth === "simple");
  });

  // ── live hydration ────────────────────────────────────────────────
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function gaugeColor(v, invert) {
    if (v == null) return "var(--dim)";
    // For most metrics a HIGH reading is bad (CPU/RAM/disk pegged); for battery it's the opposite —
    // a full battery is good — so invert the "badness" scale for it.
    var bad = invert ? 100 - v : v;
    if (bad < 60) return "var(--green)";
    if (bad < 85) return "var(--yellow)";
    return "var(--red)";
  }
  function getJSON(url) {
    return fetch(url, { headers: { "X-CSRF-Token": window.CSRF_TOKEN || "" }, credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); });
  }

  function setGauge(metric, v, num, det, invert) {
    var g = document.querySelector('.gauge[data-metric="' + metric + '"]');
    if (!g) return;
    if (v == null) {
      g.style.setProperty("--v", 0);
      g.style.setProperty("--c", "var(--dim)");
      g.querySelector(".num").textContent = "--";
      g.querySelector(".det").textContent = det || "n/a";
      return;
    }
    g.style.setProperty("--v", v);
    g.style.setProperty("--c", gaugeColor(v, invert));
    g.querySelector(".num").textContent = num;
    g.querySelector(".det").textContent = det;
  }

  function refreshHealth() {
    getJSON("/api/system-health").then(function (s) {
      setGauge("cpu", Math.round(s.cpu_percent), Math.round(s.cpu_percent), s.cpu_percent.toFixed(1) + "%");
      setGauge("ram", Math.round(s.memory_percent), Math.round(s.memory_percent),
        (s.memory_used_mb / 1024).toFixed(1) + "/" + Math.round(s.memory_total_mb / 1024) + " GB");
      setGauge("disk", Math.round(s.disk_percent), Math.round(s.disk_percent),
        s.disk_used_gb + "/" + s.disk_total_gb + " GB");
      if (s.battery_percent == null) {
        setGauge("batt", null, null, "no battery");
      } else {
        setGauge("batt", Math.round(s.battery_percent), Math.round(s.battery_percent), Math.round(s.battery_percent) + "%", true);
      }
      var gps = document.getElementById("dash-gps");
      if (gps) {
        gps.className = s.gps_fix ? "con" : "dim";
        gps.style.fontWeight = "600";
        gps.innerHTML = s.gps_fix ? "● Fix Acquired" : "○ no fix";
      }
    }).catch(function () { /* transient — keep last render */ });
  }

  function refreshDevices() {
    getJSON("/api/devices").then(function (devs) {
      var dot = function (c) { return c ? "●" : "○"; };
      var listHtml = devs.length ? devs.map(function (d) {
        return '<tr><td class="' + (d.connected ? "con" : "off") + '">' + dot(d.connected) + " " +
          esc(d.port) + " — " + esc(d.name || d.firmware || "device") + "</td></tr>";
      }).join("") : '<tr><td class="off">○ no devices — press Scan Ports</td></tr>';
      var list = document.getElementById("dash-devices");
      if (list) list.innerHTML = listHtml;

      var rowsHtml = devs.length ? devs.map(function (d) {
        return '<tr><td class="mono ' + (d.connected ? "con" : "off") + '">' + esc(d.port) + "</td><td class=\"" +
          (d.connected ? "" : "off") + '">' + esc(d.firmware || "—") + '</td><td class="r off">--</td><td class="r off">--</td></tr>';
      }).join("") : '<tr><td class="off" colspan="4">no devices attached</td></tr>';
      var rows = document.getElementById("dash-health-rows");
      if (rows) rows.innerHTML = rowsHtml;

      var count = devs.length;
      var rc = document.getElementById("rail-device-count");
      if (rc) rc.textContent = count;

      // first connected device drives Selected Device + the serial subscription
      var sel = devs.filter(function (d) { return d.connected; })[0] || null;
      subscribeSerial(sel ? sel.port : null, sel ? sel.firmware : null);
    }).catch(function () {});
  }

  function refreshTargets() {
    getJSON("/api/targets").then(function (ts) {
      var body = document.getElementById("dash-pool");
      if (body) {
        body.innerHTML = ts.length ? ts.map(function (t) {
          return "<tr><td>" + esc(t.target_type || "—") + "</td><td>" + esc(t.ssid || t.mac || "—") +
            '</td><td class="r">' + (t.rssi != null ? esc(t.rssi) : "—") + '</td><td class="r">' +
            (t.channel != null ? esc(t.channel) : "—") + "</td></tr>";
        }).join("") : '<tr><td class="off" colspan="4">pool empty — targets appear as devices discover them</td></tr>';
      }
      var xc = document.getElementById("xc-count");
      if (xc) xc.textContent = ts.length;
    }).catch(function () {});
  }

  // ── live serial (Socket.IO) ───────────────────────────────────────
  var socket = null;
  var subscribedPort = null;
  var term = document.getElementById("dash-term");

  function termLine(cls, text) {
    if (!term) return;
    var div = document.createElement("div");
    div.className = cls;
    div.textContent = text;
    term.appendChild(div);
    while (term.childNodes.length > 200) term.removeChild(term.firstChild);
    term.scrollTop = term.scrollHeight;
  }

  function subscribeSerial(port, fw) {
    var title = document.getElementById("term-title");
    var selTitle = document.getElementById("sel-title");
    if (!port) {
      if (title) title.textContent = "no device";
      if (selTitle) selTitle.textContent = "none connected";
      subscribedPort = null;
      return;
    }
    if (title) title.textContent = port + " — " + (fw || "device");
    if (selTitle) selTitle.textContent = port + " · " + (fw || "device");
    if (subscribedPort === port) return; // already streaming this port
    subscribedPort = port;
    if (term) { term.innerHTML = ""; termLine("p", "[Connected to " + port + "]"); }
    if (!socket && window.io) {
      socket = window.io({ auth: { csrf: window.CSRF_TOKEN || "" } });
      socket.on("serial_output", function (msg) {
        if (msg && msg.port === subscribedPort) termLine("rx", msg.line);
      });
    }
    if (socket) socket.emit("subscribe_serial", { port: port, csrf: window.CSRF_TOKEN || "" });
  }

  // initial hydrate + 5s cadence (matches the mockup's "live 5s")
  refreshHealth(); refreshDevices(); refreshTargets();
  setInterval(refreshHealth, 5000);
  setInterval(refreshDevices, 5000);
  setInterval(refreshTargets, 5000);
})();
