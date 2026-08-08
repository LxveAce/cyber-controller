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
    if (window.__ccPollTick) { window.__ccPollTick(); }   // instant refresh for the surface just shown
    if (v === "crack" && window.__ccRefreshCaptures) { window.__ccRefreshCaptures(); }
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

  // B9: topbar gear ⚙ → open SETTINGS (reuse the rail handler by clicking its navitem).
  var gearBtn = document.querySelector('.topbar .icon-btn[title="Settings"]');
  if (gearBtn) gearBtn.addEventListener("click", function () {
    var s = document.querySelector('.navitem[data-view="settings"]');
    if (s) s.click();
  });

  // ── live hydration ────────────────────────────────────────────────
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function ageOf(iso) {
    if (!iso) return "—";
    var t = Date.parse(iso);
    if (isNaN(t)) return "—";
    var s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 2) return "now";
    if (s < 60) return s + "s";
    if (s < 3600) return Math.round(s / 60) + "m";
    return Math.round(s / 3600) + "h";
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

  // B13: Cross-Comm pool Refresh + Clear (clears the operator's own scan results; re-scan repopulates).
  wireBtn("pool-refresh", function () { refreshTargets(); });
  wireBtn("pool-clear", function () {
    if (!window.confirm("Clear the shared target pool?\n\nThis removes the current scan results (re-scanning repopulates them).")) return;
    postJSON("/api/targets/clear", {}).then(function () { refreshTargets(); }).catch(function () {});
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
      if (window.__macroSyncDevices) window.__macroSyncDevices(devs);
      if (window.__rulesSyncDevices) window.__rulesSyncDevices(devs);
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
      if (window.__ccRefreshTails) window.__ccRefreshTails();
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
    // signal sparkline: one bar per BLE device, height scaled from RSSI (~-100..-30 dBm → 0..100%)
    var spark = document.querySelector("#hunt-ble-spark .bars");
    if (spark) {
      spark.innerHTML = ble.length ? ble.map(function (t) {
        var h = t.rssi == null ? 5 : Math.max(5, Math.min(100, Math.round((t.rssi + 100) / 70 * 100)));
        return '<i style="height:' + h + '%"></i>';
      }).join("") : "";
    }
    var br = document.getElementById("hunt-ble-rows");
    if (br) br.innerHTML = ble.length ? ble.map(function (t) {
      return "<tr><td>" + esc(t.ssid || "(unnamed)") + '</td><td class="mono dim">' + esc(t.mac || "—") +
        "</td><td>" + esc(t.vendor || "—") + '</td><td class="r">' + (t.rssi != null ? esc(t.rssi) : "—") +
        '</td><td class="r dim">' + esc(ageOf(t.last_seen)) + "</td></tr>";
    }).join("") : '<tr><td class="off" colspan="5">no BLE targets yet — scan BLE from a connected device</td></tr>';

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
    var lastConnected = null;   // last device list seen (null = not synced yet)
    // ── Local host shell (only present when /api/host-shell says enabled) ──
    var HOST = "__hostshell__";
    var hostShellEnabled = false;
    var hostOpened = false;
    var hostOutEl = null;

    function selectPort(port) {
      activePort = port;
      listEl.querySelectorAll(".termrow").forEach(function (r) { r.classList.toggle("on", r.dataset.term === port); });
      panesEl.querySelectorAll(".rterm-pane").forEach(function (p) { p.style.display = p.dataset.term === port ? "block" : "none"; });
      if (port === HOST) openHostShell();
    }
    function openHostShell() {
      if (hostOpened) return;
      hostOpened = true;
      ensureSocket();
      if (socket) socket.emit("host_shell_open", { csrf: window.CSRF_TOKEN || "" });
    }
    function buildHostPane() {
      if (built[HOST]) return;
      built[HOST] = true;
      var pane = document.createElement("div");
      pane.className = "rterm-pane"; pane.dataset.term = HOST; pane.style.display = "none";
      var head = document.createElement("div"); head.className = "between";
      head.innerHTML = '<h3 style="margin:0 0 8px"><span class="t">Local — host shell</span> <span class="dim">· this machine</span></h3>';
      var clearBtn = document.createElement("button"); clearBtn.className = "btn sm"; clearBtn.textContent = "Clear";
      var headRow = document.createElement("div"); headRow.className = "row"; headRow.appendChild(clearBtn);
      head.appendChild(headRow);
      var term = document.createElement("div"); term.className = "term termbig"; term.style.whiteSpace = "pre-wrap";
      hostOutEl = term;
      var inp = document.createElement("div"); inp.className = "terminput"; inp.style.marginTop = "8px";
      inp.innerHTML = '<span class="pr">$</span>';
      var field = document.createElement("input");
      field.className = "field mono"; field.setAttribute("aria-label", "Host command"); field.placeholder = "run a host command on this machine…";
      var btn = document.createElement("button"); btn.className = "btn"; btn.textContent = "Run";
      function doRun() {
        var cmd = field.value || "";
        if (!cmd.trim()) return;
        ensureSocket();
        if (socket) socket.emit("host_shell_input", { data: cmd + "\n", csrf: window.CSRF_TOKEN || "" });
        field.value = "";
      }
      btn.addEventListener("click", doRun);
      field.addEventListener("keydown", function (e) { if (e.key === "Enter") doRun(); });
      clearBtn.addEventListener("click", function () { if (hostOutEl) hostOutEl.textContent = ""; });
      inp.appendChild(field); inp.appendChild(btn);
      pane.appendChild(head); pane.appendChild(term); pane.appendChild(inp);
      panesEl.appendChild(pane);
    }
    function hostRowHtml() {
      return '<div class="termrow' + (activePort === HOST ? " on" : "") + '" data-term="' + HOST +
        '"><span class="sw" style="background:#c9d1d9"></span><span class="nm">Local (host shell)</span><span class="st">this machine</span></div>';
    }
    function renderList(connected) {
      var rows = hostShellEnabled ? hostRowHtml() : "";
      if (connected && connected.length) {
        rows += connected.map(function (d) {
          return '<div class="termrow' + (d.port === activePort ? " on" : "") + '" data-term="' + esc(d.port) +
            '"><span class="sw" style="background:var(--green)"></span><span class="nm">' + esc(d.port) +
            " — " + esc(d.firmware || d.name || "device") + '</span><span class="st con">● live</span></div>';
        }).join("");
      } else if (!hostShellEnabled) {
        rows += '<div class="dim" style="font-size:12px;padding:6px">no connected ports — connect a device under DEVICE ▸ Dashboard.</div>';
      }
      listEl.innerHTML = rows;
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
      lastConnected = connected;
      renderList(connected);
      connected.forEach(function (d) { buildPane(d.port, d.firmware); });
      // Keep a valid selection: prefer the current one; else the first port; else the host shell.
      var stillValid = activePort === HOST ? hostShellEnabled : connected.some(function (d) { return d.port === activePort; });
      if (!stillValid) {
        if (connected.length) selectPort(connected[0].port);
        else if (hostShellEnabled) selectPort(HOST);
        else activePort = null;
      }
    };
    listEl.addEventListener("click", function (e) {
      var r = e.target.closest(".termrow");
      if (r && r.dataset.term) selectPort(r.dataset.term);
    });

    // Host shell is opt-in + loopback-only; the probe tells us whether to offer the Local tab at all.
    getJSON("/api/host-shell").then(function (d) {
      if (!d || !d.enabled) return;
      hostShellEnabled = true;
      buildHostPane();
      ensureSocket();
      if (socket) {
        socket.on("host_shell_output", function (msg) {
          if (!msg || !hostOutEl) return;
          hostOutEl.textContent += msg.text || "";
          if (hostOutEl.textContent.length > 20000) hostOutEl.textContent = hostOutEl.textContent.slice(-20000);
          hostOutEl.scrollTop = hostOutEl.scrollHeight;
        });
      }
      renderList(lastConnected || []);            // reveal the Local row now, even before a device sync
      if (!activePort) selectPort(HOST);           // land on it if nothing else is open
    }).catch(function () { /* probe unavailable -> no Local tab, serial terminals unaffected */ });
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

  // B12: OPERATE Macros card — real saved macros from /api/macros (list only; run is device-gated).
  function initMacros() {
    var body = document.getElementById("macros-body");
    var count = document.getElementById("macros-count");
    var sel = document.getElementById("macro-device");
    var consent = document.getElementById("macro-consent");
    var status = document.getElementById("macro-status");
    var stopBtn = document.getElementById("macro-stop");
    if (!body) return;

    function setStatus(t, err) { if (status) { status.textContent = t || ""; status.style.color = err ? "var(--red)" : "var(--dim)"; } }

    function run(name, offensive) {
      if (!sel || !sel.value) { setStatus("select a connected device first", true); return; }
      if (offensive && !(consent && consent.checked)) {
        setStatus("this macro transmits — check “authorized use” to run it", true); return;
      }
      setStatus("running “" + name + "”…");
      postJSON("/api/macros/run", { name: name, port: sel.value, consent: !!(consent && consent.checked) })
        .then(function () { setStatus("started “" + name + "”"); })
        .catch(function (err) { setStatus("run failed: " + err, true); });
    }

    getJSON("/api/macros").then(function (ms) {
      if (count) count.textContent = ms.length + (ms.length === 1 ? " macro" : " macros");
      body.innerHTML = ms.length ? "" : '<tr><td class="off" colspan="4">no saved macros yet — record one in the desktop app</td></tr>';
      ms.forEach(function (m) {
        var tr = document.createElement("tr");
        var badge = m.offensive ? ' <span class="badge" style="background:#2b1416;color:var(--red)">transmits</span>' : "";
        tr.innerHTML = "<td>" + (m.secured ? "🔒 " : "") + esc(m.name) + badge +
          '</td><td class="dim">' + esc(m.protocol || "—") + '</td><td class="r">' + esc(m.step_count) + "</td>";
        var td = document.createElement("td");
        td.className = "r";
        var play = document.createElement("button");
        play.className = "btn sm green";
        play.textContent = "▶ Play";
        play.addEventListener("click", function () { run(m.name, m.offensive); });
        td.appendChild(play);
        tr.appendChild(td);
        body.appendChild(tr);
      });
    }).catch(function () { body.innerHTML = '<tr><td class="off" colspan="4">macros unavailable</td></tr>'; });

    if (stopBtn) stopBtn.addEventListener("click", function () {
      postJSON("/api/macros/stop", {}).then(function () { setStatus("stopping…"); }).catch(function () {});
    });

    // device select tracks connected devices; playback progress streams over the socket
    window.__macroSyncDevices = function (devs) {
      if (!sel) return;
      var connected = devs.filter(function (d) { return d.connected; });
      var prev = sel.value;
      sel.innerHTML = connected.length
        ? connected.map(function (d) { return '<option value="' + esc(d.port) + '">' + esc(d.port) + " — " + esc(d.firmware || d.name || "device") + "</option>"; }).join("")
        : '<option value="">no connected device</option>';
      if (connected.some(function (d) { return d.port === prev; })) sel.value = prev;
    };
    var s = ensureSocket();
    if (s) {
      s.on("macro_progress", function (d) { d = d || {}; setStatus("step " + d.step + "/" + d.total + (d.message ? " · " + d.message : "")); });
      s.on("macro_done", function (d) { d = d || {}; setStatus(d.success ? "✓ " + (d.message || "done") : "✗ " + (d.message || "failed"), !d.success); });
    }
  }
  initMacros();

  // B14 (stream half): Cross-Comm live event stream — read-only fan-out of the bus events the app
  // already emits (target_discovered / device connect+disconnect). The auto-routing RULES half stays
  // deferred (a rule can auto-fire an offensive command — that needs its own consent/gating pass).
  function initCrossCommStream() {
    var el = document.getElementById("xc-stream");
    if (!el) return;
    var s = ensureSocket();
    if (!s) return;
    var first = true;
    function line(cls, text) {
      if (first) { el.innerHTML = ""; first = false; }
      appendLine(el, cls, text);
    }
    s.on("target_discovered", function (t) {
      t = t || {};
      line("rx", "[target] " + (t.target_type || "?") + " " + (t.ssid || t.mac || "?") +
        (t.rssi != null ? " rssi=" + t.rssi : ""));
    });
    s.on("device_connected", function (d) { line("ok", "[device] connected " + ((d || {}).port || "?")); });
    s.on("device_disconnected", function (d) { line("wa", "[device] disconnected " + ((d || {}).port || "?")); });
  }
  initCrossCommStream();

  // B14 (rules half): Cross-Comm auto-routing rules — offensive rules are consent-gated on add AND
  // land disabled; arming (enabling) an offensive rule is a second consent-gated act. Mirrors the
  // server gates so the UI can't imply an offensive rule is live without the operator arming it.
  function initRules() {
    var listEl = document.getElementById("xc-rules");
    var addBtn = document.getElementById("rule-add");
    var msg = document.getElementById("rule-msg");
    var portSel = document.getElementById("rule-port");
    if (!listEl || !addBtn) return;
    function setMsg(t, err) { if (msg) { msg.textContent = t || ""; msg.style.color = err ? "var(--red)" : "var(--dim)"; } }

    function refresh() {
      getJSON("/api/rules").then(function (rules) {
        if (!rules.length) { listEl.innerHTML = '<div class="dim" style="font-size:11px">no rules</div>'; return; }
        listEl.innerHTML = "";
        rules.forEach(function (r) {
          var row = document.createElement("div");
          row.className = "between";
          row.style.cssText = "font-size:11px;padding:3px 0;border-bottom:1px solid var(--bd2)";
          var badge = r.offensive ? ' <span class="badge" style="background:#2b1416;color:var(--red)">offensive</span>' : "";
          var state = r.enabled ? '<span class="con">● armed</span>' : '<span class="dim">○ disabled</span>';
          row.innerHTML = "<span>" + esc(r.name) + badge + ' <span class="dim mono">' + esc(r.command_template) +
            " → " + esc(r.device_port) + "</span></span>";
          var ctrl = document.createElement("span");
          ctrl.style.cssText = "display:flex;gap:6px;align-items:center;white-space:nowrap";
          var toggle = document.createElement("button");
          toggle.className = "btn sm";
          toggle.innerHTML = r.enabled ? "Disable" : "Enable";
          toggle.addEventListener("click", function () {
            var body = { name: r.name, enabled: !r.enabled };
            if (!r.enabled && r.offensive) {
              if (!window.confirm("Arm the offensive rule “" + r.name + "”?\n\nIt will auto-fire “" +
                r.command_template + "” on every matching target until disabled.")) return;
              body.consent = true;
            }
            postJSON("/api/rules/toggle", body).then(refresh).catch(function (e) { setMsg(e, true); });
          });
          var rm = document.createElement("button");
          rm.className = "btn sm danger";
          rm.textContent = "×";
          rm.addEventListener("click", function () {
            postJSON("/api/rules/remove", { name: r.name }).then(refresh).catch(function () {});
          });
          ctrl.innerHTML = state + " ";
          ctrl.appendChild(toggle); ctrl.appendChild(rm);
          row.appendChild(ctrl);
          listEl.appendChild(row);
        });
      }).catch(function () { listEl.innerHTML = '<div class="dim" style="font-size:11px">rules unavailable</div>'; });
    }

    addBtn.addEventListener("click", function () {
      var name = (document.getElementById("rule-name").value || "").trim();
      var cmd = (document.getElementById("rule-cmd").value || "").trim();
      var port = portSel ? portSel.value : "";
      var consent = document.getElementById("rule-consent");
      if (!name || !cmd || !port) { setMsg("name, device + command required", true); return; }
      var body = {
        name: name, command_template: cmd, device_port: port,
        ssid_pattern: (document.getElementById("rule-ssid").value || "").trim(),
        consent: !!(consent && consent.checked),
      };
      postJSON("/api/rules", body).then(function (res) {
        setMsg(res.offensive && !res.enabled ? "added (offensive → disabled; Enable to arm)" : "added");
        document.getElementById("rule-name").value = "";
        document.getElementById("rule-cmd").value = "";
        refresh();
      }).catch(function (e) { setMsg(typeof e === "string" ? e : "add failed", true); });
    });

    window.__rulesSyncDevices = function (devs) {
      if (!portSel) return;
      var connected = devs.filter(function (d) { return d.connected; });
      var prev = portSel.value;
      portSel.innerHTML = '<option value="">device</option>' + connected.map(function (d) {
        return '<option value="' + esc(d.port) + '">' + esc(d.port) + "</option>";
      }).join("");
      if (connected.some(function (d) { return d.port === prev; })) portSel.value = prev;
    };
    refresh();
    window.__ccRefreshRules = refresh;
  }
  initRules();

  // Software-OS tab: live OS catalog + live REMOVABLE-drive detection. The destructive write (erases a
  // whole drive) is owner-gated, so Flash hands over the exact reviewed host command rather than firing
  // an unvetted web drive-erase — honest, not a dead button, not an irreversible action taken blind.
  function initSoftwareOS() {
    var osSel = document.getElementById("os-select");
    var drvSel = document.getElementById("os-drive");
    var desc = document.getElementById("os-desc");
    var log = document.getElementById("os-log");
    var flash = document.getElementById("os-flash");
    var rescan = document.getElementById("os-rescan");
    var count = document.getElementById("os-count");
    if (!osSel || !drvSel) return;
    var images = [];
    function human(sz) { return sz ? (sz / 1e9).toFixed(1) + " GB" : "?"; }

    getJSON("/api/os/images").then(function (imgs) {
      images = imgs || [];
      if (count) count.textContent = images.length + " images";
      osSel.innerHTML = images.length
        ? images.map(function (i) { return '<option value="' + esc(i.id) + '">' + esc(i.name) + " [" + esc(i.category) + "]</option>"; }).join("")
        : '<option value="">catalog unavailable</option>';
      updateDesc();
    }).catch(function () { osSel.innerHTML = '<option value="">catalog unavailable</option>'; });

    function updateDesc() {
      var img = images.filter(function (i) { return i.id === osSel.value; })[0];
      if (desc && img) desc.textContent = (img.description || "") + " — verified (SHA-256 + OpenPGP) before writing.";
    }
    osSel.addEventListener("change", updateDesc);

    function loadDrives() {
      drvSel.innerHTML = '<option value="">scanning…</option>';
      getJSON("/api/os/drives").then(function (drives) {
        drvSel.innerHTML = drives.length
          ? drives.map(function (d) { return '<option value="' + esc(d.device) + '">' + esc(d.device) + "  " + esc(d.name || "") + "  " + human(d.size) + "</option>"; }).join("")
          : '<option value="">no removable drives detected</option>';
      }).catch(function () { drvSel.innerHTML = '<option value="">drive scan unavailable</option>'; });
    }
    loadDrives();
    if (rescan) rescan.addEventListener("click", function (e) { e.preventDefault(); loadDrives(); });

    if (flash) flash.addEventListener("click", function () {
      var id = osSel.value, dev = drvSel.value;
      if (log) {
        log.innerHTML = "";
        if (!id || !dev) {
          appendLine(log, "er", "Pick an OS and a removable drive first.");
          return;
        }
        appendLine(log, "wa", "Writing an OS erases the ENTIRE drive — this is owner-gated in the web UI.");
        appendLine(log, "p", "Run this on the host (it verifies SHA-256 + OpenPGP, then writes):");
        appendLine(log, "tx", "cyber-controller --flash-os " + id + " --target " + dev);
        appendLine(log, "p", "Add --yes to skip the confirm. Only removable drives are ever accepted.");
      }
    });
  }
  initSoftwareOS();

  // B11: HUNT Tail Detect — live follower/persistence table from /api/tails (fed by the pool's BLE +
  // client discoveries). Awareness-only; empty until a device reappears across windows.
  function refreshTails() {
    var body = document.getElementById("tail-rows");
    var summary = document.getElementById("tail-summary");
    if (!body) return;
    getJSON("/api/tails").then(function (hits) {
      if (summary) {
        summary.textContent = hits.length
          ? hits.length + (hits.length === 1 ? " device keeps reappearing" : " devices keep reappearing")
          : "none reappearing";
        summary.style.color = hits.length ? "var(--orange)" : "var(--dim)";
      }
      body.innerHTML = hits.length ? hits.map(function (h) {
        var col = h.persistence >= 0.9 ? "var(--red)" : (h.persistence >= 0.7 ? "var(--orange)" : "var(--amber)");
        return '<tr><td class="mono">' + esc(h.device) + "</td><td>" + esc(h.label || "—") +
          '</td><td class="r" style="color:' + col + '">' + h.persistence.toFixed(2) +
          '</td><td class="r">' + esc(h.windows) + "</td></tr>";
      }).join("") : '<tr><td class="off" colspan="4">no persistent followers — a device must reappear across time windows to flag</td></tr>';
    }).catch(function () {});
  }
  window.__ccRefreshTails = refreshTails;
  refreshTails();

  // ── CRACK: live engine detection + optional-tool fetch (the RUN stays consent-gated) ─
  function crackToolRow(a) {
    var mark = a.present ? "✓" : "✗";
    var col = a.present ? "var(--green)" : "var(--dim)";
    var where = a.present ? (a.source === "PATH" ? "on PATH" : (a.source === "installed" ? "installed" : "ready")) : "not installed";
    var btn = (!a.present && a.can_autofetch) ? '<button class="btn sm" data-install="' + esc(a.tool) + '">Install</button>' : "";
    return '<div class="row" style="align-items:center;margin:4px 0;font-size:11px">' +
      '<span style="color:' + col + ';min-width:170px">' + mark + " " + esc(a.tool) + " · " + where + "</span>" +
      '<span class="dim" style="flex:1">' + esc(a.guidance || "") + "</span>" + btn + "</div>";
  }
  function renderCrackPanel(panel) {
    var avail = panel.__avail || [];
    if (!avail.length) {
      panel.innerHTML = '<div class="dim" style="font-size:11px">No optional tools to fetch here — the built-in native cracker is always ready.</div>';
      return;
    }
    panel.innerHTML =
      '<div class="dim" style="font-size:11px;margin-bottom:4px">Optional accelerators (the native cracker needs none of these):</div>' +
      avail.map(crackToolRow).join("") +
      '<div id="crack-panel-msg" class="dim" style="font-size:11px;margin-top:4px"></div>';
    var msg = panel.querySelector("#crack-panel-msg");
    panel.querySelectorAll("button[data-install]").forEach(function (b) {
      b.addEventListener("click", function () {
        var tool = b.getAttribute("data-install");
        b.disabled = true; b.textContent = "Installing…";
        if (msg) { msg.style.color = "var(--dim)"; msg.textContent = "Fetching " + tool + "… (download + verify + extract)"; }
        postJSON("/api/crack/install-tool", { tool: tool }).then(function (res) {
          if (msg) { msg.style.color = "var(--green)"; msg.textContent = "✓ " + tool + " installed → " + (res.path || "tools dir"); }
          initCrack();
        }).catch(function (err) {
          b.disabled = false; b.textContent = "Install";
          if (msg) { msg.style.color = "var(--amber)"; msg.textContent = "✗ " + (typeof err === "string" ? err : "install failed"); }
        });
      });
    });
  }
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
      var panel = document.getElementById("crack-tools-panel");
      if (panel) { panel.__avail = d.availability || []; if (!panel.hidden) renderCrackPanel(panel); }
    }).catch(function () { foot.textContent = "engine detection unavailable"; });
  }
  (function wireCrack() {
    var getBtn = document.getElementById("crack-get-tools");
    var reBtn = document.getElementById("crack-recheck");
    var panel = document.getElementById("crack-tools-panel");
    if (getBtn && panel) getBtn.addEventListener("click", function () {
      panel.hidden = !panel.hidden;
      if (!panel.hidden) renderCrackPanel(panel);
    });
    if (reBtn) reBtn.addEventListener("click", function () { initCrack(); });
    initCrack();
  })();

  // ── CRACK ▸ Wordlists: bundled + installed picker, on-demand catalog download, bring-your-own ─
  function initWordlists() {
    var sel = document.getElementById("wl-select");
    if (!sel) return;
    var panel = document.getElementById("wl-catalog-panel");
    var foot = document.getElementById("wl-foot");
    function wlOption(w, group) {
      var size = w.size_human ? " (" + esc(w.size_human) + ")" : "";
      return '<option value="' + esc(w.path || w.name) + '">' + esc(w.name) + size + (group ? " · " + group : "") + "</option>";
    }
    function load() {
      getJSON("/api/wordlists").then(function (d) {
        var opts = [];
        (d.bundled || []).forEach(function (w) { opts.push(wlOption(w, "bundled")); });
        (d.installed || []).forEach(function (w) { opts.push(wlOption(w, "installed")); });
        sel.innerHTML = opts.length ? opts.join("") : '<option>no wordlists &#8212; Get more&#8230; or Bring your own&#8230;</option>';
        if (foot && d.dir) foot.textContent = "Wordlists in " + d.dir + " · the bundled WPA core works offline.";
        if (panel) { panel.__catalog = d.catalog || []; if (!panel.hidden) renderCatalog(); }
      }).catch(function () { sel.innerHTML = '<option>wordlist listing unavailable</option>'; });
    }
    function renderCatalog() {
      var cat = panel.__catalog || [];
      panel.innerHTML = '<div class="dim" style="font-size:11px;margin-bottom:4px">Downloadable wordlists (integrity-checked on install):</div>' +
        cat.map(function (c) {
          var right = c.installed
            ? '<span style="color:var(--green)">&#10003; installed</span>'
            : '<button class="btn sm" data-dl="' + esc(c.id) + '">Download ' + esc(c.size_human) + "</button>";
          return '<div class="row" style="align-items:center;margin:4px 0;font-size:11px">' +
            '<span style="min-width:160px">' + esc(c.name) + ' <span class="dim">&#183; ' + esc(c.category) + "</span></span>" +
            '<span class="dim" style="flex:1">' + esc(c.description || "") + "</span>" + right + "</div>";
        }).join("") + '<div id="wl-msg" class="dim" style="font-size:11px;margin-top:4px"></div>';
      var msg = panel.querySelector("#wl-msg");
      panel.querySelectorAll("button[data-dl]").forEach(function (b) {
        b.addEventListener("click", function () {
          var id = b.getAttribute("data-dl");
          b.disabled = true; b.textContent = "Downloading…";
          if (msg) { msg.style.color = "var(--dim)"; msg.textContent = "Fetching " + id + "… (large lists take a while)"; }
          postJSON("/api/wordlists/download", { id: id }).then(function () {
            if (msg) { msg.style.color = "var(--green)"; msg.textContent = "✓ " + id + " installed"; }
            load();
          }).catch(function (err) {
            b.disabled = false; b.textContent = "Download";
            if (msg) { msg.style.color = "var(--amber)"; msg.textContent = "✗ " + (typeof err === "string" ? err : "download failed"); }
          });
        });
      });
    }
    var catBtn = document.getElementById("wl-catalog");
    if (catBtn && panel) catBtn.addEventListener("click", function () {
      panel.hidden = !panel.hidden;
      if (!panel.hidden) renderCatalog();
    });
    var byoRow = document.getElementById("wl-byo-row");
    var byoBtn = document.getElementById("wl-byo");
    if (byoBtn && byoRow) byoBtn.addEventListener("click", function () { byoRow.hidden = !byoRow.hidden; });
    var byoAdd = document.getElementById("wl-byo-add");
    var byoPath = document.getElementById("wl-byo-path");
    function addByo() {
      var p = (byoPath.value || "").trim();
      if (!p) return;
      postJSON("/api/wordlists/byo", { path: p }).then(function () {
        if (foot) { foot.style.color = "var(--green)"; foot.textContent = "✓ added " + p; }
        byoPath.value = ""; byoRow.hidden = true; load();
      }).catch(function (err) {
        if (foot) { foot.style.color = "var(--amber)"; foot.textContent = "✗ " + (typeof err === "string" ? err : "couldn't add that file"); }
      });
    }
    if (byoAdd) byoAdd.addEventListener("click", addByo);
    if (byoPath) byoPath.addEventListener("keydown", function (e) { if (e.key === "Enter") addByo(); });
    var refreshBtn = document.getElementById("wl-refresh");
    if (refreshBtn) refreshBtn.addEventListener("click", load);
    load();
  }
  initWordlists();

  // ── CRACK ▸ Run state (shared by the captures table + the Run card) ─
  var crackSel = null;   // selected capture: { key, ssid, bssid, crackable }
  function crackUpdateRunEnabled() {
    var runBtn = document.getElementById("crack-run");
    var consent = document.getElementById("crack-consent");
    if (!runBtn) return;
    var ok = !!(crackSel && crackSel.crackable && consent && consent.checked);
    runBtn.disabled = !ok;
    runBtn.style.opacity = ok ? "1" : ".6";
  }
  function crackSelect(c) {
    crackSel = c;
    var selEl = document.getElementById("crack-sel");
    if (selEl) {
      if (!c) selEl.textContent = "Select a captured handshake below to load it into the run.";
      else if (!c.crackable) { selEl.style.color = "var(--amber)"; selEl.textContent = "“" + (c.ssid || c.bssid) + "” has no local capture to crack yet — retrieve its .pcap first."; }
      else { selEl.style.color = "var(--dim)"; selEl.textContent = "Loaded: " + (c.ssid || c.bssid) + " (" + c.type + ")"; }
    }
    crackUpdateRunEnabled();
  }

  // ── CRACK ▸ Captured Handshakes: live from the shared CaptureStore (click a crackable row to load it) ─
  function initCaptures() {
    var body = document.getElementById("crack-captures-body");
    if (!body) return;
    function pwCell(c) {
      if (c.crack_status === "cracked" && c.password) return '<td class="con mono">' + esc(c.password) + "</td>";
      if (c.crack_status === "running") return '<td style="color:var(--amber)">cracking&#8230;</td>';
      return '<td class="dim">&#8212;</td>';
    }
    var lastList = [];
    function render(list) {
      lastList = list;
      if (!list.length) {
        body.innerHTML = '<tr><td class="off" colspan="6">no captures yet &#8212; they auto-log as your devices capture handshakes / PMKIDs</td></tr>';
        return;
      }
      body.innerHTML = list.map(function (c, i) {
        var st = [];
        if (c.crackable) st.push("cursor:pointer");
        if (crackSel && crackSel.key === c.key) st.push("background:#141b24");
        var style = st.length ? ' style="' + st.join(";") + '"' : "";
        return "<tr data-i=\"" + i + "\"" + style + "><td>" + esc(c.ssid || "—") + '</td><td class="mono dim">' + esc(c.bssid || "—") +
          "</td><td>" + esc(c.type || "—") + '</td><td class="mono">' + esc(c.source || "—") +
          '</td><td class="dim">' + esc(c.captured || "—") + "</td>" + pwCell(c) + "</tr>";
      }).join("");
    }
    body.addEventListener("click", function (e) {
      var tr = e.target.closest("tr[data-i]");
      if (!tr) return;
      var c = lastList[parseInt(tr.getAttribute("data-i"), 10)];
      if (c && c.crackable) { crackSelect(c); render(lastList); }
    });
    function load() {
      getJSON("/api/captures").then(function (d) {
        render(d.captures || []);
        // keep the loaded capture's live status (e.g. it just cracked) in sync
        if (crackSel) {
          var still = (d.captures || []).filter(function (x) { return x.key === crackSel.key; })[0];
          if (!still) crackSelect(null);
        }
      }).catch(function () { body.innerHTML = '<tr><td class="er" colspan="6">captures unavailable</td></tr>'; });
    }
    window.__ccRefreshCaptures = load;   // the rail handler re-pulls this when CRACK is opened
    load();
  }
  initCaptures();

  // ── CRACK ▸ Run: consent-gated native crack, streamed to the Log card ─
  function initCrackRun() {
    var runBtn = document.getElementById("crack-run");
    var stopBtn = document.getElementById("crack-stop");
    var consent = document.getElementById("crack-consent");
    var logEl = document.getElementById("crack-log");
    if (!runBtn) return;
    if (consent) consent.addEventListener("change", crackUpdateRunEnabled);
    function logLine(text, cls) {
      if (!logEl) return;
      var d = document.createElement("div");
      d.className = cls || "rx";
      d.textContent = text;
      logEl.appendChild(d);
      while (logEl.childNodes.length > 300) logEl.removeChild(logEl.firstChild);
      logEl.scrollTop = logEl.scrollHeight;
    }
    function running(on) {
      if (stopBtn) { stopBtn.disabled = !on; stopBtn.style.opacity = on ? "1" : ".5"; }
      if (on) { runBtn.disabled = true; runBtn.style.opacity = ".6"; } else { crackUpdateRunEnabled(); }
    }
    ensureSocket();
    if (socket) {
      socket.on("crack_log", function (m) { if (m) logLine(m.line || ""); });
      socket.on("crack_done", function (m) {
        if (m && m.cracked) logLine("RECOVERED → " + (m.ssid || "") + " : " + (m.password || ""), "ok");
        else if (m) logLine("[done] " + (m.detail || "key not in wordlist"), "wa");
        running(false);
        if (window.__ccRefreshCaptures) window.__ccRefreshCaptures();
      });
    }
    runBtn.addEventListener("click", function () {
      if (!crackSel || !consent || !consent.checked) return;
      var wl = document.getElementById("wl-select");
      var wordlist = wl && wl.value ? wl.value : "";
      if (logEl) logEl.innerHTML = "";
      logLine("[launch] " + (crackSel.ssid || crackSel.bssid), "tx");
      running(true);
      postJSON("/api/crack/run", { consent: true, capture_key: crackSel.key, wordlist: wordlist, engine: "native", bssid: crackSel.bssid || "" })
        .catch(function (err) { logLine("✗ " + (typeof err === "string" ? err : "run refused"), "wa"); running(false); });
    });
    if (stopBtn) stopBtn.addEventListener("click", function () {
      postJSON("/api/crack/stop", {}).then(function () { logLine("[stopping…]", "wa"); }).catch(function () {});
    });
    crackUpdateRunEnabled();
  }
  initCrackRun();

  // ── Mesh ▸ Provisioned Nodes: live vault status + management (keys redacted server-side) ─
  // Renders the key-free node table with per-node actions (Rotate / Deprovision / Attach / Detach) and a
  // Provision form, wired to the existing /api/nodes/* endpoints. Rotate + Deprovision are two-click confirm
  // (destructive: a key rotation / a registration removal); Attach/Detach are reversible so they fire direct.
  function initNodes() {
    var body = document.getElementById("mesh-nodes-body");
    var state = document.getElementById("mesh-nodes-state");
    if (!body) return;
    getJSON("/api/nodes-status").then(function (d) {
      if (state) state.textContent = d.unlocked ? "unlocked" : "locked";
      if (!d.unlocked) {
        body.innerHTML = '<div class="card2" style="text-align:center;color:var(--mut);padding:22px;border:1px dashed var(--bd);border-radius:7px">🔒  Unlock the access gate to manage nodes.</div>';
        return;
      }
      renderNodes(body, d.rows || [], d.gateways || []);
    }).catch(function () { body.innerHTML = '<div class="er" style="font-size:12px;padding:6px">nodes status unavailable</div>'; });
  }
  function renderNodes(body, rows, gateways) {
    var gwOpts = gateways.map(function (g) { return '<option>' + esc(g) + '</option>'; }).join("");
    var tableRows = rows.length ? rows.map(function (r) {
      var id = r.node_id != null ? r.node_id : "—";
      var attached = !!r.attached;
      var stateTxt = attached ? '<span class="con">&#9679; attached</span>' : '<span class="off">&#9675; detached</span>';
      var gw = attached ? esc(r.port || "—") : "—";
      var attachBtn = attached
        ? '<button class="btn sm" data-act="detach" data-id="' + esc(id) + '">Detach</button>'
        : '<button class="btn sm" data-act="attach" data-id="' + esc(id) + '"' + (gwOpts ? '' : ' disabled title="open a gateway in Devices first"') + '>Attach</button>';
      return '<tr><td class="mono">' + esc(id) + '</td><td>' + esc(r.label || "—") + '</td><td class="dim">' + esc(r.role || "—") +
        '</td><td>' + stateTxt + ' <span class="mono dim">' + gw + '</span></td>' +
        '<td class="r"><div class="row" style="justify-content:flex-end;gap:5px">' + attachBtn +
        '<button class="btn warn sm" data-act="rotate" data-id="' + esc(id) + '">Rotate</button>' +
        '<button class="btn danger sm" data-act="deprovision" data-id="' + esc(id) + '">Deprovision</button>' +
        '</div></td></tr>';
    }).join("") : '<tr><td class="off" colspan="5">No provisioned nodes yet — provision one below.</td></tr>';

    body.innerHTML =
      '<table><thead><tr><th>Node</th><th>Label</th><th>Role</th><th>State</th><th class="r">Actions</th></tr></thead><tbody>' + tableRows + '</tbody></table>' +
      '<div class="row" style="margin-top:10px;align-items:center;gap:6px;flex-wrap:wrap">' +
        '<label class="dim" style="font-size:11px">Attach gateway</label>' +
        '<select class="field" id="mesh-gw" style="width:auto"' + (gwOpts ? '' : ' disabled') + '>' + (gwOpts || '<option>no open gateway</option>') + '</select>' +
        '<span class="dim" style="font-size:11px">— open a gateway in Devices, then Attach a node over it</span>' +
      '</div>' +
      '<div class="row" style="margin-top:8px;align-items:center;gap:6px;flex-wrap:wrap">' +
        '<input class="field mono" id="mesh-prov-id" placeholder="node id 0–65535" style="width:120px" inputmode="numeric">' +
        '<select class="field" id="mesh-prov-role" style="width:auto"><option value="host">host</option><option value="node">node</option></select>' +
        '<input class="field" id="mesh-prov-label" placeholder="label (optional)" maxlength="64" style="width:150px">' +
        '<button class="btn green sm" id="mesh-prov-btn">Provision&#8230;</button>' +
      '</div>' +
      '<div id="mesh-nodes-msg" class="footnote" style="min-height:14px;margin-top:6px"></div>';

    var msg = body.querySelector("#mesh-nodes-msg");
    var gwSel = body.querySelector("#mesh-gw");
    function act(path, payload) {
      if (msg) { msg.style.color = "var(--dim)"; msg.textContent = "working…"; }
      postJSON(path, payload).then(function () {
        if (msg) { msg.style.color = "var(--green)"; msg.textContent = "✓ done"; }
        initNodes();
      }).catch(function (err) {
        if (msg) { msg.style.color = "var(--amber)"; msg.textContent = "✗ " + (typeof err === "string" ? err : "action failed"); }
      });
    }
    body.querySelectorAll("button[data-act]").forEach(function (b) {
      b.addEventListener("click", function () {
        var actName = b.getAttribute("data-act");
        var id = parseInt(b.getAttribute("data-id"), 10);
        if (actName === "detach") { act("/api/nodes/detach", { node_id: id }); return; }
        if (actName === "attach") {
          var gw = gwSel && gwSel.value;
          if (!gw || gwSel.disabled) { if (msg) { msg.style.color = "var(--amber)"; msg.textContent = "open a gateway in Devices first"; } return; }
          act("/api/nodes/attach", { node_id: id, gateway_port: gw }); return;
        }
        // rotate / deprovision — two-click confirm (matches the card's "each confirm" promise)
        if (b.dataset.armed === "1") { act("/api/nodes/" + actName, { node_id: id }); return; }
        var orig = b.textContent;
        b.dataset.armed = "1";
        b.textContent = "Confirm " + orig + "?";
        setTimeout(function () { if (b.dataset.armed === "1") { b.dataset.armed = ""; b.textContent = orig; } }, 4000);
      });
    });
    var provBtn = body.querySelector("#mesh-prov-btn");
    if (provBtn) provBtn.addEventListener("click", function () {
      var raw = (body.querySelector("#mesh-prov-id").value || "").trim();
      if (!/^\d+$/.test(raw) || parseInt(raw, 10) > 65535) {
        if (msg) { msg.style.color = "var(--amber)"; msg.textContent = "node id must be a whole number 0–65535"; }
        return;
      }
      act("/api/nodes/provision", {
        node_id: parseInt(raw, 10),
        role: body.querySelector("#mesh-prov-role").value,
        label: (body.querySelector("#mesh-prov-label").value || "").trim(),
      });
    });
  }
  initNodes();

  // ── MAP ▸ Flock/ALPR: OSINT camera import (user-initiated, awareness-only) ─
  function initFlock() {
    var input = document.getElementById("flock-bbox");
    var btn = document.getElementById("flock-load");
    var msg = document.getElementById("flock-msg");
    var count = document.getElementById("flock-count");
    var attr = document.getElementById("flock-attr");
    var markers = document.getElementById("flock-markers");
    if (!btn || !markers) return;
    var SVGNS = "http://www.w3.org/2000/svg";

    function render(cams, bbox) {
      while (markers.firstChild) markers.removeChild(markers.firstChild);
      var s = bbox[0], w = bbox[1], n = bbox[2], e = bbox[3];
      var lonSpan = (e - w) || 1e-6, latSpan = (n - s) || 1e-6;
      cams.forEach(function (c) {
        var x = ((c.lon - w) / lonSpan) * 1000;
        var y = (1 - (c.lat - s) / latSpan) * 380;   // lat up = y down
        var halo = document.createElementNS(SVGNS, "circle");
        halo.setAttribute("cx", x); halo.setAttribute("cy", y); halo.setAttribute("r", 14);
        halo.setAttribute("fill", "#f85149"); halo.setAttribute("opacity", "0.12");
        var dot = document.createElementNS(SVGNS, "circle");
        dot.setAttribute("cx", x); dot.setAttribute("cy", y); dot.setAttribute("r", 4.5);
        dot.setAttribute("fill", "#f85149");
        if (c.label) { var t = document.createElementNS(SVGNS, "title"); t.textContent = c.label; dot.appendChild(t); }
        markers.appendChild(halo); markers.appendChild(dot);
      });
    }
    btn.addEventListener("click", function () {
      var raw = (input.value || "").trim();
      if (raw.split(",").length !== 4) { msg.textContent = "enter S,W,N,E"; msg.style.color = "var(--red)"; return; }
      msg.style.color = "var(--dim)"; msg.textContent = "importing from OpenStreetMap…";
      btn.disabled = true;
      getJSON("/api/flock?bbox=" + encodeURIComponent(raw)).then(function (d) {
        var bbox = raw.split(",").map(Number);
        render(d.cameras || [], bbox);
        if (count) count.textContent = d.count + " ALPR cameras";
        msg.textContent = d.count ? "" : "no ALPR cameras in that area";
        if (attr) attr.textContent = d.attribution || "";
      }).catch(function (err) {
        msg.style.color = "var(--red)";
        msg.textContent = typeof err === "string" ? err : "import failed (offline or rate-limited)";
      }).then(function () { btn.disabled = false; });
    });
  }
  initFlock();

  // MAP Wardrive: honest gating (no dead buttons). A survey needs a connected GPS-equipped device
  // running wardrive firmware; there's no web wardrive backend yet, so the buttons say so plainly
  // rather than doing nothing. (Honest-functionality audit, 2026-08-07.)
  function initWardrive() {
    var msg = document.getElementById("wd-msg");
    var multiMsg = document.getElementById("wd-multi-msg");
    var need = "Wardrive needs a connected GPS-equipped device running wardrive firmware. Connect one " +
      "under DEVICE, then survey + export to WiGLE CSV. (Web wardrive wiring lands with that device.)";
    wireBtn("wd-start", function () { if (msg) msg.textContent = need; });
    wireBtn("wd-export", function () { if (msg) msg.textContent = "No survey to export yet — start a GPS survey first."; });
    wireBtn("wd-multi-start", function () { if (multiMsg) multiMsg.textContent = "Needs 2+ connected GPS devices."; });
  }
  initWardrive();

  // Surface-gated, visibility-aware live cadence (P0-6): one tick that refreshes ONLY the visible
  // surface's data, and stops entirely while the window is hidden/minimized — so an idle or
  // backgrounded app costs nothing on hardware. (The mockup's "live 5s", only where it's on screen.)
  refreshHealth(); refreshDevices(); refreshTargets();   // initial hydrate (once, seeds every surface)
  function _activeView() {
    var el = document.querySelector(".main .view.on");
    return el ? el.dataset.view : "";
  }
  function _pollTick() {
    var v = _activeView();
    if (v === "device") { refreshHealth(); refreshDevices(); refreshTargets(); }
    else if (v === "hunt") { refreshTargets(); }
    // other surfaces are socket-driven or static — no periodic poll needed
  }
  var _pollTimer = null;
  function _startPoll() { if (!_pollTimer) _pollTimer = setInterval(_pollTick, 5000); }
  function _stopPoll() { if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; } }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { _stopPoll(); }
    else { _pollTick(); _startPoll(); }   // catch-up on re-show, then resume
  });
  window.__ccPollTick = _pollTick;         // let the nav handlers force an instant refresh on switch
  if (!document.hidden) { _startPoll(); }
})();
