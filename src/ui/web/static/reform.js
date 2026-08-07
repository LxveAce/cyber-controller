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

  // The desktop shell (and any Basic-auth deep link) loads this page with credentials in the URL
  // (http://user:pass@host/…). The browser then REFUSES every relative fetch() with "Request cannot be
  // constructed from a URL that includes credentials", which would break all live hydration + actions.
  // The first load already authenticated and set the session cookie, so drop the credentials from the
  // address and let the cookie carry auth — fetch() works on the clean URL.
  if (location.username || location.password) {
    location.replace(location.origin + location.pathname + location.search + location.hash);
    return;
  }

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
  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": window.CSRF_TOKEN || "" },
      credentials: "same-origin",
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      return r.json().then(function (data) { return r.ok ? data : Promise.reject(data.error || r.status); });
    });
  }

  // ── Dashboard device actions (Connect / Disconnect / Scan) ─────────
  var selectedPort = null;
  var devMsg = document.getElementById("dash-dev-msg");
  function setDevMsg(text, isErr) {
    if (!devMsg) return;
    devMsg.textContent = text || "";
    devMsg.style.color = isErr ? "var(--red)" : "var(--dim)";
  }
  var devTable = document.getElementById("dash-devices");
  if (devTable) {
    devTable.addEventListener("click", function (e) {
      var row = e.target.closest("tr.dev-row");
      if (!row || !row.dataset.port) return;
      selectedPort = row.dataset.port;
      document.querySelectorAll("#dash-devices .dev-row").forEach(function (r) {
        r.classList.toggle("sel", r === row);
      });
      setDevMsg("selected " + selectedPort);
    });
  }
  function wireBtn(id, fn) {
    var b = document.getElementById(id);
    if (b) b.addEventListener("click", fn);
  }
  wireBtn("dash-connect", function () {
    if (!selectedPort) { setDevMsg("click a device row first", true); return; }
    setDevMsg("connecting " + selectedPort + "…");
    postJSON("/api/connect", { port: selectedPort })
      .then(function () { setDevMsg("connected " + selectedPort); refreshDevices(); })
      .catch(function (err) { setDevMsg("connect failed: " + err, true); });
  });
  wireBtn("dash-disconnect", function () {
    if (!selectedPort) { setDevMsg("click a device row first", true); return; }
    setDevMsg("disconnecting " + selectedPort + "…");
    postJSON("/api/disconnect", { port: selectedPort })
      .then(function () { setDevMsg("disconnected " + selectedPort); refreshDevices(); })
      .catch(function (err) { setDevMsg("disconnect failed: " + err, true); });
  });
  wireBtn("dash-scan", function () {
    setDevMsg("scanning ports…");
    refreshDevices();
    setTimeout(function () { setDevMsg(""); }, 1200);
  });

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
        var sel = d.port === selectedPort ? " sel" : "";
        return '<tr data-port="' + esc(d.port) + '" class="dev-row' + sel + '"><td class="' +
          (d.connected ? "con" : "off") + '">' + dot(d.connected) + " " +
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
      if (window.__opSyncDevices) window.__opSyncDevices(devs);
      if (window.__fwSyncPorts) window.__fwSyncPorts(devs);
      if (window.__termSyncDevices) window.__termSyncDevices(devs);
    }).catch(function () {});
  }

  function refreshTargets() {
    getJSON("/api/targets").then(function (ts) {
      var body = document.getElementById("dash-pool");
      if (body) {
        var TLABELS = { ap: "AP", client: "Client", ble: "BLE", subghz: "SubGHz", nfc: "NFC", rfid: "RFID", alpr: "ALPR" };
        body.innerHTML = ts.length ? ts.map(function (t) {
          return "<tr><td>" + esc(TLABELS[t.target_type] || t.target_type || "—") + "</td><td>" + esc(t.ssid || t.mac || "—") +
            '</td><td class="r">' + (t.rssi != null ? esc(t.rssi) : "—") + '</td><td class="r">' +
            (t.channel != null ? esc(t.channel) : "—") + "</td></tr>";
        }).join("") : '<tr><td class="off" colspan="4">pool empty — targets appear as devices discover them</td></tr>';
      }
      var xc = document.getElementById("xc-count");
      if (xc) xc.textContent = ts.length;
      renderHunt(ts);
    }).catch(function () {});
  }

  // HUNT surfaces are derived from the shared target pool (real discovered targets, no separate backend).
  function tile(cls, n, label) {
    return '<div class="tile ' + cls + '"><div class="n">' + esc(n) + '</div><div class="l">' + esc(label) + "</div></div>";
  }
  function renderHunt(ts) {
    var wifi = ts.filter(function (t) { return t.target_type === "ap" || t.target_type === "client"; });
    var aps = ts.filter(function (t) { return t.target_type === "ap"; });
    var clients = ts.filter(function (t) { return t.target_type === "client"; });
    var open = aps.filter(function (t) { return (t.encryption || "").toUpperCase() === "OPEN"; });
    var ble = ts.filter(function (t) { return t.target_type === "ble"; });

    var wt = document.getElementById("hunt-wifi-tiles");
    if (wt) wt.innerHTML = tile("green", aps.length, "APs") + tile("", clients.length, "Clients") +
      tile("orange", open.length, "Open");
    var wr = document.getElementById("hunt-wifi-rows");
    if (wr) wr.innerHTML = wifi.length ? wifi.map(function (t) {
      return "<tr><td>" + esc(t.ssid || "(hidden)") + '</td><td class="mono dim">' + esc(t.mac || "—") +
        '</td><td class="r">' + (t.channel != null ? esc(t.channel) : "—") + '</td><td class="r">' +
        (t.rssi != null ? esc(t.rssi) : "—") + "</td><td>" + esc(t.encryption || "—") + "</td></tr>";
    }).join("") : '<tr><td class="off" colspan="5">no Wi-Fi targets yet — scan from a connected device</td></tr>';

    var strongest = ble.reduce(function (m, t) { return (t.rssi != null && t.rssi > m) ? t.rssi : m; }, -999);
    var named = ble.filter(function (t) { return t.ssid; });
    var bt = document.getElementById("hunt-ble-tiles");
    if (bt) bt.innerHTML = tile("green", ble.length, "Present") + tile("", named.length, "Named") +
      tile("green", strongest > -999 ? strongest : "—", "Strongest");
    var br = document.getElementById("hunt-ble-rows");
    if (br) br.innerHTML = ble.length ? ble.map(function (t) {
      return "<tr><td>" + esc(t.ssid || "(unnamed)") + '</td><td class="mono dim">' + esc(t.mac || "—") +
        "</td><td>" + esc(t.vendor || "—") + '</td><td class="r">' + (t.rssi != null ? esc(t.rssi) : "—") + "</td></tr>";
    }).join("") : '<tr><td class="off" colspan="4">no BLE targets yet — scan BLE from a connected device</td></tr>';

    var TL = { ap: "AP", client: "Client", ble: "BLE", subghz: "SubGHz", nfc: "NFC", rfid: "RFID", alpr: "ALPR" };
    var tc = document.getElementById("hunt-tgt-count");
    if (tc) tc.textContent = ts.length;
    var tr = document.getElementById("hunt-targets-rows");
    if (tr) tr.innerHTML = ts.length ? ts.map(function (t) {
      return "<tr><td>" + esc(TL[t.target_type] || t.target_type || "—") + "</td><td>" + esc(t.ssid || "—") +
        '</td><td class="mono dim">' + esc(t.mac || "—") + '</td><td class="r">' + (t.rssi != null ? esc(t.rssi) : "—") +
        '</td><td class="r">' + (t.channel != null ? esc(t.channel) : "—") + "</td><td class=\"mono\">" + esc(t.device_source || "—") + "</td></tr>";
    }).join("") : '<tr><td class="off" colspan="6">pool empty</td></tr>';
  }

  // ── live serial (Socket.IO), shared by every terminal sink ─────────
  // One socket; serial_output fans out to any registered sink whose port matches. Both the Dashboard
  // terminal and the OPERATE Activity terminal register as sinks, so each shows its device's output.
  var socket = null;
  var serialSinks = {};   // port -> [DOM elements]
  var subscribedPorts = {};

  function appendLine(el, cls, text) {
    if (!el) return;
    var div = document.createElement("div");
    div.className = cls;
    div.textContent = text;
    el.appendChild(div);
    while (el.childNodes.length > 300) el.removeChild(el.firstChild);
    el.scrollTop = el.scrollHeight;
  }
  function ensureSocket() {
    if (socket || !window.io) return socket;
    socket = window.io({ auth: { csrf: window.CSRF_TOKEN || "" } });
    socket.on("serial_output", function (msg) {
      if (!msg || !msg.port) return;
      (serialSinks[msg.port] || []).forEach(function (el) { appendLine(el, "rx", msg.line); });
    });
    return socket;
  }
  function subscribePort(port) {
    if (!port) return;
    ensureSocket();
    if (!subscribedPorts[port] && socket) {
      subscribedPorts[port] = true;
      socket.emit("subscribe_serial", { port: port, csrf: window.CSRF_TOKEN || "" });
    }
  }
  function bindTerminal(port, el, resetLine) {
    if (!port || !el) return;
    serialSinks[port] = serialSinks[port] || [];
    if (serialSinks[port].indexOf(el) === -1) {
      serialSinks[port].push(el);
      if (resetLine) { el.innerHTML = ""; appendLine(el, "p", resetLine); }
    }
    subscribePort(port);
  }

  // Dashboard terminal: bind to the first connected device's stream.
  var dashTerm = document.getElementById("dash-term");
  var dashBoundPort = null;
  function subscribeSerial(port, fw) {
    var title = document.getElementById("term-title");
    var selTitle = document.getElementById("sel-title");
    if (!port) {
      if (title) title.textContent = "no device";
      if (selTitle) selTitle.textContent = "none connected";
      return;
    }
    if (title) title.textContent = port + " — " + (fw || "device");
    if (selTitle) selTitle.textContent = port + " · " + (fw || "device");
    if (dashBoundPort === port) return;
    dashBoundPort = port;
    bindTerminal(port, dashTerm, "[Connected to " + port + "]");
  }

  // ── OPERATE console (device select · command grid · send · activity) ─
  function sendCommand(port, command, termEl, statusEl) {
    postJSON("/api/command", { port: port, command: command })
      .then(function () {
        appendLine(termEl, "tx", "> " + command);
        if (statusEl) statusEl.textContent = "» sent: " + command;
      })
      .catch(function (err) {
        appendLine(termEl, "er", "error: " + err);
        if (statusEl) statusEl.textContent = "error: " + err;
      });
  }
  function renderCmdGrid(groups, port, gridEl, termEl, statusEl) {
    if (!groups || !groups.length) {
      gridEl.innerHTML = '<div class="dim" style="font-size:12px">This firmware has no one-tap commands — use the raw input below.</div>';
      return;
    }
    gridEl.innerHTML = groups.map(function (g) {
      var btns = g.commands.map(function (c) {
        var dcls = c.danger ? " danger" : "";
        var badge = c.danger ? ' <span class="badge" style="background:#2a2109;color:var(--amber)">' + esc(c.danger) + "</span>" : "";
        return '<button class="cmdbtn' + dcls + '" data-cmd="' + esc(c.command) + '" data-danger="' +
          esc(c.danger || "") + '">' + esc(c.label) + badge + '<span class="raw">' + esc(c.command) + "</span></button>";
      }).join("");
      return '<div class="cat"><h4>' + esc(g.category) + '</h4><div class="cmdgrid">' + btns + "</div></div>";
    }).join("");
    gridEl.querySelectorAll(".cmdbtn").forEach(function (b) {
      b.addEventListener("click", function () {
        var cmd = b.getAttribute("data-cmd");
        var danger = b.getAttribute("data-danger");
        if (danger && !window.confirm("Controlled / authorized use only (" + danger + "):\n\n" + cmd + "\n\nProceed?")) return;
        sendCommand(port, cmd, termEl, statusEl);
      });
    });
  }
  function initOperate() {
    var sel = document.getElementById("op-device");
    var grid = document.getElementById("op-cmdgrid");
    var termEl = document.getElementById("op-term");
    var statusEl = document.getElementById("op-status");
    var fwEl = document.getElementById("op-fw");
    var input = document.getElementById("op-input");
    var sendBtn = document.getElementById("op-send");
    if (!sel || !grid) return;

    function loadFor(port) {
      if (!port) { grid.innerHTML = '<div class="dim" style="font-size:12px">Connect a device (DEVICE ▸ Dashboard) to load its command set.</div>'; if (fwEl) fwEl.textContent = "—"; return; }
      bindTerminal(port, termEl, "[Activity — " + port + "]");
      getJSON("/api/quick-commands?port=" + encodeURIComponent(port)).then(function (data) {
        if (fwEl) fwEl.textContent = data.firmware || "device";
        renderCmdGrid(data.groups, port, grid, termEl, statusEl);
      }).catch(function () { grid.innerHTML = '<div class="er" style="font-size:12px">Could not load commands.</div>'; });
    }
    sel.addEventListener("change", function () { loadFor(sel.value); });
    if (sendBtn) sendBtn.addEventListener("click", function () {
      var cmd = (input.value || "").trim();
      if (!cmd || !sel.value) { if (statusEl) statusEl.textContent = "select a device + type a command"; return; }
      sendCommand(sel.value, cmd, termEl, statusEl);
      input.value = "";
    });
    if (input) input.addEventListener("keydown", function (e) { if (e.key === "Enter" && sendBtn) sendBtn.click(); });

    // keep the device dropdown in sync with connected devices
    window.__opSyncDevices = function (devs) {
      var connected = devs.filter(function (d) { return d.connected; });
      var prev = sel.value;
      sel.innerHTML = connected.length
        ? connected.map(function (d) { return '<option value="' + esc(d.port) + '">' + esc(d.port) + " — " + esc(d.firmware || d.name || "device") + "</option>"; }).join("")
        : '<option value="">no connected device</option>';
      var want = connected.some(function (d) { return d.port === prev; }) ? prev : (connected[0] ? connected[0].port : "");
      sel.value = want;
      if (want !== prev || !grid.dataset.loaded) { grid.dataset.loaded = "1"; loadFor(want); }
    };
  }
  initOperate();

  // ── DEVICE ▸ Firmware (flash, wired to /api/flash + /api/variants) ──
  function initFirmware() {
    var portsBody = document.getElementById("fw-ports");
    var portMsg = document.getElementById("fw-port-msg");
    var logEl = document.getElementById("fw-log");
    var bar = document.getElementById("fw-bar");
    if (!portsBody) return;
    var fwPort = null;

    window.__fwSyncPorts = function (devs) {
      if (!devs.length) { portsBody.innerHTML = '<tr><td class="off">no ports — plug in a device</td></tr>'; return; }
      portsBody.innerHTML = devs.map(function (d) {
        var sel = d.port === fwPort ? " sel" : "";
        return '<tr data-port="' + esc(d.port) + '" class="dev-row' + sel + '"><td class="' +
          (d.connected ? "con" : "off") + '">' + (d.connected ? "●" : "○") + " " + esc(d.port) +
          " — " + esc(d.name || d.firmware || "device") + "</td></tr>";
      }).join("");
    };
    portsBody.addEventListener("click", function (e) {
      var row = e.target.closest("tr.dev-row");
      if (!row || !row.dataset.port) return;
      fwPort = row.dataset.port;
      portsBody.querySelectorAll(".dev-row").forEach(function (r) { r.classList.toggle("sel", r === row); });
      if (portMsg) portMsg.textContent = "target: " + fwPort;
    });

    // lazy-load each profile's variants on first interaction
    document.querySelectorAll(".fw-variant").forEach(function (sel) {
      var loaded = false;
      sel.addEventListener("focus", function () {
        if (loaded) return;
        loaded = true;
        getJSON("/api/variants?profile=" + encodeURIComponent(sel.dataset.profile)).then(function (data) {
          (data.variants || []).forEach(function (v) {
            var o = document.createElement("option");
            o.value = v.name;
            o.textContent = v.label ? v.name + " — " + v.label : v.name;
            sel.appendChild(o);
          });
        }).catch(function () {});
      });
    });

    document.querySelectorAll(".fw-flash").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (!fwPort) { if (portMsg) { portMsg.textContent = "click a target port first"; portMsg.style.color = "var(--red)"; } return; }
        var profile = btn.dataset.profile;
        var vsel = document.querySelector('.fw-variant[data-profile="' + profile + '"]');
        var variant = vsel ? vsel.value : "";
        if (!window.confirm("Flash " + profile + " to " + fwPort + "?\n\nThis overwrites the device firmware and cannot be undone mid-write.")) return;
        if (bar) bar.style.width = "0";
        if (logEl) { logEl.innerHTML = ""; appendLine(logEl, "p", "[Flashing " + profile + " → " + fwPort + "]"); }
        ensureSocket();
        postJSON("/api/flash", { port: fwPort, profile_id: profile, variant: variant })
          .then(function () { appendLine(logEl, "tx", "flash started…"); })
          .catch(function (err) { appendLine(logEl, "er", "error: " + err); });
      });
    });

    // flash progress stream
    ensureSocket();
    if (socket) socket.on("flash_progress", function (d) {
      if (!d || (fwPort && d.port && d.port !== fwPort)) return;
      if (bar && typeof d.percent === "number") bar.style.width = d.percent + "%";
      if (d.message) appendLine(logEl, d.done ? (d.success ? "ok" : "er") : "rx", d.message);
    });
  }
  initFirmware();

  // ── TERMINAL view: one live terminal per connected port ────────────
  function initTerminal() {
    var listEl = document.getElementById("reform-termlist");
    var panesEl = document.getElementById("reform-termpanes");
    if (!listEl || !panesEl) return;
    var built = {};   // port -> true (pane already built)
    var activePort = null;

    function selectPort(port) {
      activePort = port;
      listEl.querySelectorAll(".termrow").forEach(function (r) { r.classList.toggle("on", r.dataset.term === port); });
      panesEl.querySelectorAll(".rterm-pane").forEach(function (p) { p.style.display = p.dataset.term === port ? "block" : "none"; });
    }
    function buildPane(port, fw) {
      if (built[port]) return;
      built[port] = true;
      var pane = document.createElement("div");
      pane.className = "rterm-pane";
      pane.dataset.term = port;
      pane.style.display = "none";
      var head = document.createElement("div");
      head.className = "between";
      head.innerHTML = '<h3 style="margin:0 0 8px"><span class="t">' + esc(port) + "</span> <span class=\"dim\">· " + esc(fw || "serial") + "</span></h3>";
      var term = document.createElement("div");
      term.className = "term termbig";
      var inp = document.createElement("div");
      inp.className = "terminput";
      inp.style.marginTop = "8px";
      inp.innerHTML = '<span class="pr">&gt;</span>';
      var field = document.createElement("input");
      field.className = "field mono";
      field.setAttribute("aria-label", "Command for " + port);
      field.placeholder = "type a raw command (danger verbs still confirm)…";
      var btn = document.createElement("button");
      btn.className = "btn";
      btn.textContent = "Send";
      function doSend() {
        var cmd = (field.value || "").trim();
        if (!cmd) return;
        sendCommand(port, cmd, term, null);
        field.value = "";
      }
      btn.addEventListener("click", doSend);
      field.addEventListener("keydown", function (e) { if (e.key === "Enter") doSend(); });
      inp.appendChild(field); inp.appendChild(btn);
      pane.appendChild(head); pane.appendChild(term); pane.appendChild(inp);
      panesEl.appendChild(pane);
      bindTerminal(port, term, "[Live terminal — " + port + "]");
    }

    window.__termSyncDevices = function (devs) {
      var connected = devs.filter(function (d) { return d.connected; });
      if (!connected.length) {
        listEl.innerHTML = '<div class="dim" style="font-size:12px;padding:6px">no connected ports — connect a device under DEVICE ▸ Dashboard.</div>';
        return;
      }
      listEl.innerHTML = connected.map(function (d) {
        return '<div class="termrow' + (d.port === activePort ? " on" : "") + '" data-term="' + esc(d.port) +
          '"><span class="sw" style="background:var(--green)"></span><span class="nm">' + esc(d.port) +
          " — " + esc(d.firmware || d.name || "device") + '</span><span class="st con">● live</span></div>';
      }).join("");
      connected.forEach(function (d) { buildPane(d.port, d.firmware); });
      if (!activePort || !connected.some(function (d) { return d.port === activePort; })) {
        selectPort(connected[0].port);
      }
    };
    listEl.addEventListener("click", function (e) {
      var r = e.target.closest(".termrow");
      if (r && r.dataset.term) selectPort(r.dataset.term);
    });
  }
  initTerminal();

  // ── SETTINGS: live access-gate status (read-only, no secrets) ───────
  function initSettings() {
    var el = document.getElementById("set-gate");
    if (!el) return;
    getJSON("/api/gate-status").then(function (g) {
      var factors = [];
      if (g.has_password) factors.push("password");
      if (g.has_key) factors.push("USB key");
      el.textContent = "configured=" + g.configured + " · policy=" + g.policy +
        " · factors=" + (factors.join("+") || "none") + (g.locked ? " · LOCKED (" + g.remaining_secs + "s)" : "");
    }).catch(function () { el.textContent = "gate status unavailable"; });
  }
  initSettings();

  // ── CRACK: live engine detection (read-only; the RUN stays consent-gated) ─
  function initCrack() {
    var eng = document.getElementById("crack-engine");
    var foot = document.getElementById("crack-tools");
    if (!eng || !foot) return;
    getJSON("/api/crack-tools").then(function (d) {
      var backends = d.backends || [];
      var labels = { native: "Built-in native cracker", hashcat: "hashcat (GPU)", aircrack: "aircrack-ng (CPU)" };
      eng.innerHTML = backends.map(function (b) { return '<option>' + esc(labels[b] || b) + "</option>"; }).join("");
      foot.innerHTML = (d.tools || []).map(function (t) {
        var mark = t.present ? "✓" : "✗";
        var col = t.present ? "var(--green)" : "var(--dim)";
        return '<span style="color:' + col + '">' + mark + " " + esc(t.name) + (t.present && t.version ? " " + esc(t.version.split(" ")[0]) : "") + "</span>";
      }).join(" · ") || "native ready";
    }).catch(function () { foot.textContent = "engine detection unavailable"; });
  }
  initCrack();

  // initial hydrate + 5s cadence (matches the mockup's "live 5s")
  refreshHealth(); refreshDevices(); refreshTargets();
  setInterval(refreshHealth, 5000);
  setInterval(refreshDevices, 5000);
  setInterval(refreshTargets, 5000);
})();
