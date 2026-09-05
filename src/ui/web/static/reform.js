/* Cyber Controller — reform web shell behavior.
 *
 * Two jobs:
 *   1) the mockup's own navigation (rail views, sub-tabs, depth toggle, terminal picker) — verbatim logic
 *      from the mockup so the shell feels identical;
 *   2) LIVE hydration of DEVICE ▸ Dashboard from the real Flask/core endpoints (/api/system-health,
 *      /api/devices, /api/targets) + the authenticated Socket.IO serial stream. Everything else is the
 *      static design preview that a later phase wires up the same way.
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

  // The rail is a real tablist so keyboard/switch users can reach every surface, not just mouse
  // users (WCAG 2.1.1 keyboard + 4.1.2 name/role/value). Roving tabindex: only the active tab is in
  // the tab order; arrows move between tabs and activate them.
  var rail = document.getElementById("rail");
  var navitems = Array.prototype.slice.call(rail.querySelectorAll(".navitem"));
  rail.setAttribute("role", "tablist");
  rail.setAttribute("aria-orientation", "vertical");
  rail.setAttribute("aria-label", "Primary");
  navitems.forEach(function (n) {
    var on = n.classList.contains("on");
    n.setAttribute("role", "tab");
    n.setAttribute("tabindex", on ? "0" : "-1");
    n.setAttribute("aria-selected", on ? "true" : "false");
    var g = n.querySelector(".g");
    if (g) g.setAttribute("aria-hidden", "true");   // decorative glyph — the label carries the name
  });

  function activateNav(it, focusIt) {
    if (!it) return;
    var v = it.dataset.view;
    navitems.forEach(function (n) {
      var on = n === it;
      n.classList.toggle("on", on);
      n.setAttribute("aria-selected", on ? "true" : "false");
      n.setAttribute("tabindex", on ? "0" : "-1");
    });
    document.querySelectorAll(".main .view").forEach(function (sec) { sec.classList.toggle("on", sec.dataset.view === v); });
    var view = document.querySelector('.view[data-view="' + v + '"]');
    var tabs = view.querySelector(".subtabs button.on");
    var subName = tabs ? tabs.textContent : "";
    crumb.innerHTML = "<b>" + crumbNames[v] + "</b>" + (subName ? " ▸ " + subName : "");
    document.getElementById("main").scrollTop = 0;
    // Reflect the active surface in the URL (no scroll jump) so "Pop out" can deep-link to it.
    if (window.history && history.replaceState) history.replaceState(null, "", "#" + v);
    if (window.__ccPollTick) { window.__ccPollTick(); }   // instant refresh for the surface just shown
    if (v === "crack" && window.__ccRefreshCaptures) { window.__ccRefreshCaptures(); }
    if (focusIt) it.focus();
  }

  rail.addEventListener("click", function (e) {
    var it = e.target.closest(".navitem");
    if (it) activateNav(it, false);
  });
  rail.addEventListener("keydown", function (e) {
    var cur = document.activeElement && document.activeElement.closest ? document.activeElement.closest(".navitem") : null;
    var idx = navitems.indexOf(cur);
    if (idx < 0) return;
    var next = -1;
    if (e.key === "ArrowDown" || e.key === "ArrowRight") next = (idx + 1) % navitems.length;
    else if (e.key === "ArrowUp" || e.key === "ArrowLeft") next = (idx - 1 + navitems.length) % navitems.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = navitems.length - 1;
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activateNav(navitems[idx], true); return; }
    else return;
    e.preventDefault();
    activateNav(navitems[next], true);
  });

  document.querySelectorAll(".subtabs").forEach(function (bar) {
    bar.setAttribute("role", "tablist");
    bar.querySelectorAll("button").forEach(function (x) {
      x.setAttribute("role", "tab");
      x.setAttribute("aria-selected", x.classList.contains("on") ? "true" : "false");
    });
    bar.addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (!b) return;
      var scope = bar.parentElement;
      bar.querySelectorAll("button").forEach(function (x) {
        var on = x === b;
        x.classList.toggle("on", on);
        x.setAttribute("aria-selected", on ? "true" : "false");
      });
      scope.querySelectorAll(":scope > .sub").forEach(function (s) { s.classList.toggle("on", s.dataset.sub === b.dataset.sub); });
      if (bar.dataset.tabs && crumbNames[scope.dataset.view]) {
        crumb.innerHTML = "<b>" + crumbNames[scope.dataset.view] + "</b> ▸ " + b.textContent;
      }
    });
  });

  document.querySelectorAll("#depth button").forEach(function (x) {
    x.setAttribute("aria-pressed", x.classList.contains("on") ? "true" : "false");
  });
  document.getElementById("depth").addEventListener("click", function (e) {
    var b = e.target.closest("button");
    if (!b) return;
    document.querySelectorAll("#depth button").forEach(function (x) {
      var on = x === b;
      x.classList.toggle("on", on);
      x.setAttribute("aria-pressed", on ? "true" : "false");
    });
    var simple = b.dataset.depth === "simple";
    document.getElementById("app").classList.toggle("pro-hidden", simple);
    // Simple mode hides .pro-tab buttons (CSS); if a pro-tab was the active one its pane is now hidden
    // with nothing shown, so fall back to the first non-pro tab in each affected subtab bar.
    if (simple) {
      document.querySelectorAll(".subtabs").forEach(function (bar) {
        var active = bar.querySelector("button.on");
        if (active && active.classList.contains("pro-tab")) {
          var firstPlain = bar.querySelector("button:not(.pro-tab)");
          if (firstPlain) firstPlain.click();
        }
      });
    }
  });

  // topbar gear ⚙ → open SETTINGS (reuse the rail handler by clicking its navitem).
  var gearBtn = document.querySelector('.topbar .icon-btn[title="Settings"]');
  if (gearBtn) gearBtn.addEventListener("click", function () {
    var s = document.querySelector('.navitem[data-view="settings"]');
    if (s) s.click();
  });

  // F2 armed-state: the SAFE lamps flip to ARMED whenever a transmit-consent is currently affirmed
  // (Broadcast or Macro "authorized use"). There is no persistent hidden arm — the lamps track the
  // live consent checkboxes and revert to SAFE the instant they're cleared, so the operator always
  // sees whether transmitting actions are armed. safety.py's per-action gate is unaffected; this is
  // an honest visual mirror, not a new control.
  function setArmed(on) {
    ["lamp-top", "armlamp-sel", "armlamp-op"].forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      el.classList.toggle("armed", on);
      var t = el.querySelector(".lt");
      if (t) t.textContent = on ? "ARMED" : "SAFE";
    });
  }
  function refreshArmed() {
    var bc = document.getElementById("bc-consent");
    var mc = document.getElementById("macro-consent");
    setArmed(!!((bc && bc.checked) || (mc && mc.checked)));
  }
  ["bc-consent", "macro-consent"].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener("change", refreshArmed);
  });

  // Raw free-text sends promise "danger verbs still confirm" — this is that confirm. It mirrors the
  // command-grid's data-danger friction for typed commands (the grid buttons already confirm via the
  // server's danger label). Substring match on transmitting-verb tokens; it errs toward asking, since
  // a UX prompt that over-confirms is safe. It is NOT a security gate (safety.py is the server floor);
  // it just makes the raw inputs honor the promise printed under them.
  var _DANGER_HINTS = ["deauth", "disassoc", "attack", "jam", "beacon", "spam", "flood", "evil",
    "portal", "karma", "pwn", "inject", "spoof", "rickroll", "badusb", "subghz tx", "rfid emulate",
    "nfc emulate"];
  function looksDangerous(cmd) {
    var c = (cmd || "").trim().toLowerCase();
    return _DANGER_HINTS.some(function (w) { return c.indexOf(w) !== -1; });
  }
  function confirmIfDangerous(cmd) {
    if (!looksDangerous(cmd)) return true;
    return window.confirm("This looks like a transmitting / offensive command:\n\n" + cmd +
      "\n\nControlled / authorized use only. Proceed?");
  }

  // Native file bridge (desktop shell only, #14): when the QtWebEngine QWebChannel bridge is present,
  // open a REAL OS file dialog and hand back the true absolute path. On the browser build
  // window.ccbridge is undefined, so callers fall back to the manual <input>/text field. Feature-
  // detected per call (the bridge publishes itself on 'ccbridge-ready', well before any user click).
  function hasNativeBridge() {
    return !!(window.ccbridge && typeof window.ccbridge.pick === "function");
  }
  function nativePick(kind, cb) {
    if (!hasNativeBridge()) return false;
    window.ccbridge.pick(kind, function (path) { if (path) cb(path); });
    return true;
  }
  // Reveal any element that only makes sense with the native bridge (a Browse button), once ready.
  function revealNativeControls() {
    if (!hasNativeBridge()) return;
    document.querySelectorAll("[data-native-only]").forEach(function (el) { el.hidden = false; });
  }
  window.addEventListener("ccbridge-ready", revealNativeControls);

  // Desktop notifications (#19): a long op finishing while the operator's attention is elsewhere
  // (flash done, key recovered, macro finished). Native path = the QtWebEngine tray via the bridge;
  // browser path = the web Notifications API (best-effort, permission-gated); otherwise silent. Never
  // throws — a notification is a courtesy, never load-bearing.
  var _notifyAsked = false;
  function notifyDesktop(title, body) {
    try {
      if (window.ccbridge && typeof window.ccbridge.notify === "function") {
        window.ccbridge.notify(String(title || "Cyber Controller"), String(body || ""));
        return;
      }
      if (typeof Notification === "undefined") return;
      if (Notification.permission === "granted") {
        new Notification(title || "Cyber Controller", { body: body || "" });
      } else if (Notification.permission === "default" && !_notifyAsked) {
        _notifyAsked = true;   // ask once; if granted, later events notify
        Notification.requestPermission().then(function (p) {
          if (p === "granted") new Notification(title || "Cyber Controller", { body: body || "" });
        }).catch(function () {});
      }
    } catch (e) { /* notifications are courtesy-only — never break the app over one */ }
  }
  window.__ccNotify = notifyDesktop;   // exposed so any surface (+ tests) can fire a desktop notice

  // Topbar "Pop out (detach)" → open the CURRENT surface in its own window, deep-linked by #view.
  // Same-origin, so the new window shares this session's auth cookie (no re-login in a browser).
  var popBtn = document.querySelector('.topbar .icon-btn[title="Pop out (detach)"]');
  if (popBtn) popBtn.addEventListener("click", function () {
    var on = document.querySelector('.rail .navitem.on');
    var v = on ? on.dataset.view : "device";
    window.open(location.pathname + "#" + v, "cc_" + v, "width=1200,height=820");
  });

  // Deep-link on load: if the URL carries #<view>, open that surface (used by Pop out).
  (function () {
    var h = (location.hash || "").replace(/^#/, "");
    if (h && crumbNames[h]) {
      var it = document.querySelector('.rail .navitem[data-view="' + h + '"]');
      if (it) it.click();
    }
  })();

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
  var lastDevices = [];
  var devMsg = document.getElementById("dash-dev-msg");
  function setDevMsg(text, isErr) {
    if (!devMsg) return;
    devMsg.textContent = text || "";
    devMsg.style.color = isErr ? "var(--red)" : "var(--dim)";
  }
  // selectedPort is the ACTION target (Connect/Disconnect). It's whatever row the user last clicked and
  // PERSISTS even while that device is disconnected — only a port that has left the inventory entirely is
  // dropped. The serial terminal can only stream from a CONNECTED device, so the stream device is chosen
  // separately (the selection if it's connected, else the first connected device) WITHOUT moving the
  // action target — otherwise a disconnected selection got silently overwritten and Connect targeted the
  // wrong port / refused with "click a device row first" (N01/F01/F04).
  function applySelectedDevice() {
    if (selectedPort && !lastDevices.some(function (d) { return d.port === selectedPort; })) {
      selectedPort = null;   // the selected port is gone from the inventory — only then drop it
    }
    document.querySelectorAll("#dash-devices .dev-row").forEach(function (r) {
      r.classList.toggle("sel", r.dataset.port === selectedPort);
    });
    var selDev = lastDevices.filter(function (d) { return d.port === selectedPort; })[0] || null;
    var streamDev = (selDev && selDev.connected)
      ? selDev
      : lastDevices.filter(function (d) { return d.connected; })[0] || null;
    updateSelectedPanel(selDev);
    subscribeSerial(streamDev ? streamDev.port : null, streamDev ? streamDev.firmware : null);
  }
  // The "Selected Device" panel reflects the ACTION target (what's highlighted + what Connect will use),
  // including when it's disconnected — so panel, highlighted row and status message all agree (N01).
  function updateSelectedPanel(selDev) {
    var selTitle = document.getElementById("sel-title");
    if (!selTitle) return;
    if (!selectedPort) { selTitle.textContent = "none selected"; return; }
    var fw = selDev && (selDev.firmware || selDev.name);
    selTitle.textContent = selectedPort + " · " +
      (selDev && selDev.connected ? (fw || "device") : "disconnected");
  }
  var devTable = document.getElementById("dash-devices");
  if (devTable) {
    devTable.addEventListener("click", function (e) {
      var row = e.target.closest("tr.dev-row");
      if (!row || !row.dataset.port) return;
      selectedPort = row.dataset.port;
      setDevMsg("selected " + selectedPort);
      applySelectedDevice();
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
      .then(function (r) {
        var fw = r && r.firmware;
        setDevMsg("connected " + selectedPort + (fw ? " — detected " + fw : " — no firmware detected"));
        refreshDevices();
      })
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
    // Clear when the scan actually returns, not on a fixed timer that can blank a slow scan's result.
    refreshDevices().then(function () { setDevMsg(""); }).catch(function () { setDevMsg(""); });
  });

  // Cross-Comm pool Refresh + Clear (clears the operator's own scan results; re-scan repopulates).
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
      // cpu_stale: the sampler hasn't posted a fresh reading — show "stale" rather than a misleading number.
      if (s.cpu_stale) {
        setGauge("cpu", 0, 0, "stale");
      } else {
        setGauge("cpu", Math.round(s.cpu_percent), Math.round(s.cpu_percent), s.cpu_percent.toFixed(1) + "%");
      }
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
    return getJSON("/api/devices").then(function (devs) {
      // Detect connect-state transitions vs the previous inventory so serial subscriptions survive a
      // same-port reconnect (N02): a port that just dropped forgets its stale subscription; a port that
      // just came back (or a still-connected port that vanished then returned) re-subscribes its sinks.
      var wasConnected = {};
      lastDevices.forEach(function (d) { wasConnected[d.port] = d.connected; });
      devs.forEach(function (d) {
        if (wasConnected[d.port] === true && !d.connected) invalidateSubscription(d.port);
        if (wasConnected[d.port] === false && d.connected) resubscribeSubscription(d.port);
      });
      Object.keys(wasConnected).forEach(function (p) {
        if (wasConnected[p] && !devs.some(function (d) { return d.port === p; })) invalidateSubscription(p);
      });
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

      // the SELECTED (else first) connected device drives Selected Device + the serial subscription
      lastDevices = devs;
      applySelectedDevice();
      if (window.__opSyncDevices) window.__opSyncDevices(devs);
      if (window.__fwSyncPorts) window.__fwSyncPorts(devs);
      if (window.__termSyncDevices) window.__termSyncDevices(devs);
      if (window.__macroSyncDevices) window.__macroSyncDevices(devs);
      if (window.__rulesSyncDevices) window.__rulesSyncDevices(devs);
      if (window.__bcSyncDevices) window.__bcSyncDevices(devs);
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
        '</td><td class="dim">' + esc(t.vendor || "—") + '</td><td class="r">' + (t.channel != null ? esc(t.channel) : "—") + '</td><td class="r">' +
        (t.rssi != null ? esc(t.rssi) : "—") + "</td><td>" + esc(t.encryption || "—") + "</td></tr>";
    }).join("") : '<tr><td class="off" colspan="6">no Wi-Fi targets yet — scan from a connected device</td></tr>';

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
        '</td><td class="mono dim">' + esc(t.mac || "—") + '</td><td class="dim">' + esc(t.vendor || "—") + '</td><td class="r">' + (t.rssi != null ? esc(t.rssi) : "—") +
        '</td><td class="r">' + (t.channel != null ? esc(t.channel) : "—") + "</td><td class=\"mono\">" + esc(t.device_source || "—") + "</td></tr>";
    }).join("") : '<tr><td class="off" colspan="7">pool empty</td></tr>';
  }

  // ── live serial (Socket.IO), shared by every terminal sink ─────────
  // One socket; serial_output fans out to any registered sink whose port matches. Both the Dashboard
  // terminal and the OPERATE Activity terminal register as sinks, so each shows its device's output.
  var socket = null;
  var serialSinks = {};   // port -> [DOM elements]
  var subscribedPorts = {};
  // Set true after THIS tab's own password save/clear response has updated our session cookie. The server
  // revokes the old-generation cookie and actively disconnects this tab's socket; Socket.IO does NOT
  // auto-reconnect after a server-initiated disconnect ("io server disconnect"), so we reconnect ONCE
  // ourselves — only for our own deliberate password change, never for a victim revoked with a stale
  // cookie (whose reconnect the server would just refuse). Bounded to a single attempt: no retry loop.
  var reconnectAfterAuthChange = false;

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
    // Pin to long-polling. The server runs Flask-SocketIO in async_mode="threading" (Werkzeug), which does
    // NOT reliably serve the WebSocket transport (and simple-websocket isn't bundled in the frozen build).
    // Left on the default, the client upgrades polling -> websocket, the upgrade fails to hold, and the
    // connection goes dead right after the handshake — so flash_progress / crack_log / serial_output events
    // silently never arrive (a flash showed "nothing"). Polling is plenty for this event volume.
    socket = window.io({ auth: { csrf: window.CSRF_TOKEN || "" }, transports: ["polling"], upgrade: false });
    socket.on("serial_output", function (msg) {
      if (!msg || !msg.port) return;
      (serialSinks[msg.port] || []).forEach(function (el) { appendLine(el, "rx", msg.line); });
    });
    socket.on("connect", function () {
      // A (re)connected socket session has NO server-side serial subscriptions — the server only attaches
      // its emit callback when subscribe_serial arrives. After a dropped polling session, re-subscribe
      // every port we're still sinking so output resumes instead of going silently dead (N02).
      subscribedPorts = {};
      Object.keys(serialSinks).forEach(function (p) {
        if (serialSinks[p] && serialSinks[p].length) subscribePort(p);
      });
    });
    socket.on("disconnect", function (reason) {
      // The server actively disconnects THIS tab's socket when this operator changes/clears the web
      // password (its old-generation cookie is revoked). On a server-initiated disconnect Socket.IO reports
      // reason "io server disconnect" and does NOT auto-reconnect, so without this the live streams stay
      // dead even though the page is still authenticated over HTTP. Reconnect exactly once, and only once
      // our own password response has re-stamped the cookie (reconnectAfterAuthChange) so the new polling
      // handshake carries the new generation. A transport-level drop (any other reason) is left to the
      // Socket.IO manager's own reconnection, exactly as before.
      if (reason === "io server disconnect" && reconnectAfterAuthChange) {
        reconnectAfterAuthChange = false;
        if (socket) socket.connect();   // the "connect" handler above then re-subscribes every open sink
      }
    });
    return socket;
  }
  function recoverSocketAfterAuthChange() {
    // Called right after a password save/clear HTTP response has updated our session cookie. The revoking
    // server disconnect and this response can land in either order, so handle both: if the socket is still
    // up, arm the flag and let the disconnect handler reconnect when the server disconnect arrives; if it
    // is already down, reconnect now with the fresh cookie. A single bounded attempt — never a loop.
    if (!socket) return;                   // nothing was streaming; a later ensureSocket() connects fresh
    if (socket.connected) {
      reconnectAfterAuthChange = true;     // server disconnect still in flight — reconnect when it lands
    } else {
      reconnectAfterAuthChange = false;
      socket.connect();                    // already disconnected — reconnect immediately with new cookie
    }
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
  function unbindTerminalEl(el) {
    // Drop this DOM sink from EVERY port so a shared terminal (Dashboard / OPERATE) stops receiving a
    // previously-bound device's output after it switches devices (F02) — output from A must not appear
    // under B.
    if (!el) return;
    Object.keys(serialSinks).forEach(function (p) {
      var i = serialSinks[p].indexOf(el);
      if (i !== -1) serialSinks[p].splice(i, 1);
    });
  }
  // The client-side subscribe cache assumes a port stays subscribed for the socket's life. It doesn't:
  // when a device drops and reconnects on the SAME port, the server has a brand-new connection with no
  // callback, so the stale "already subscribed" flag left serial output dead. On a drop, forget the flag
  // (and free the dashboard bind so a same-port reconnect re-binds); on a reconnect, re-subscribe every
  // sink still attached to that port so it lands on the fresh connection (N02).
  function invalidateSubscription(port) {
    delete subscribedPorts[port];
    if (dashBoundPort === port) dashBoundPort = null;
  }
  function resubscribeSubscription(port) {
    delete subscribedPorts[port];
    if (serialSinks[port] && serialSinks[port].length) subscribePort(port);
  }

  // Dashboard terminal: bind to the SELECTED (else first connected) device's stream.
  var dashTerm = document.getElementById("dash-term");
  var dashBoundPort = null;
  function subscribeSerial(port, fw) {
    // Owns the terminal-stream header only; the Selected Device panel is driven by updateSelectedPanel so
    // the panel can show the (possibly disconnected) action target while the terminal streams elsewhere.
    var title = document.getElementById("term-title");
    if (!port) {
      if (title) title.textContent = "no device";
      unbindTerminalEl(dashTerm);
      dashBoundPort = null;
      return;
    }
    if (title) title.textContent = port + " — " + (fw || "device");
    if (dashBoundPort === port) return;
    unbindTerminalEl(dashTerm);   // release the old port before binding the new one
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
        var title = c.native ? ' title="' + esc(c.native) + '"' : "";
        return '<button class="cmdbtn' + dcls + '" data-cmd="' + esc(c.command) + '" data-danger="' +
          esc(c.danger || "") + '"' + title + ">" + esc(c.label) + badge +
          '<span class="raw">' + esc(c.command) + "</span></button>";
      }).join("");
      // g.category is now a canonical bucket (Scanning/Attack/Network/Other).
      var hcls = g.category === "Attack" ? ' class="atk"' : "";
      return '<div class="cat"><h4' + hcls + ">" + esc(g.category) + '</h4><div class="cmdgrid">' + btns + "</div></div>";
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
      unbindTerminalEl(termEl);   // release the previously-selected device so its output can't leak here (F02)
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
      if (!confirmIfDangerous(cmd)) return;
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

    // Firmware search + category filter over the collapsible groups (presentation only — never changes
    // what flashes). Filters rows, hides empty groups, and auto-opens groups with matches while searching.
    (function () {
      var search = document.getElementById("fw-search");
      var catSel = document.getElementById("fw-cat");
      var noMatch = document.getElementById("fw-nomatch");
      var collapseBtn = document.getElementById("fw-collapse-all");
      var rows = Array.prototype.slice.call(document.querySelectorAll(".fwrow[data-name]"));
      var groups = Array.prototype.slice.call(document.querySelectorAll(".fwgroup"));
      if (!rows.length) return;
      function apply() {
        var q = ((search && search.value) || "").trim().toLowerCase();
        var cat = (catSel && catSel.value) || "";
        var filtering = !!(q || cat);
        var shown = 0;
        rows.forEach(function (r) {
          var ok = (!q || r.dataset.name.indexOf(q) !== -1) && (!cat || r.dataset.cat === cat);
          r.style.display = ok ? "" : "none";
          if (ok) shown++;
        });
        // Hide groups with no visible rows; while filtering, force-open the ones that still have matches.
        groups.forEach(function (g) {
          var any = Array.prototype.some.call(g.querySelectorAll(".fwrow"), function (r) {
            return r.style.display !== "none";
          });
          g.style.display = any ? "" : "none";
          if (filtering && any) g.open = true;
        });
        if (noMatch) noMatch.style.display = shown ? "none" : "";
      }
      if (search) search.addEventListener("input", apply);
      if (catSel) catSel.addEventListener("change", apply);
      if (collapseBtn) collapseBtn.addEventListener("click", function () {
        var anyOpen = groups.some(function (g) { return g.open && g.style.display !== "none"; });
        groups.forEach(function (g) { g.open = !anyOpen; });
        collapseBtn.textContent = anyOpen ? "Expand all" : "Collapse all";
      });
    })();

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

    var statusEl = document.getElementById("fw-status");
    var pctEl = document.getElementById("fw-pct");
    var bootCallout = document.getElementById("fw-bootcallout");
    var flashWatchdog = null;   // fires if the progress stream stays silent after a flash starts
    var sawProgress = false;

    // esptool markers that mean "the board never entered the ROM bootloader" — the hold-BOOT case.
    var DL_MARKERS = ["failed to connect", "wrong boot mode", "no serial data received",
                      "invalid head of packet", "a fatal error occurred", "could not open"];
    function isDownloadModeFail(msg) {
      var m = (msg || "").toLowerCase();
      return DL_MARKERS.some(function (k) { return m.indexOf(k) !== -1; });
    }
    // Turn a raw esptool line into a short phase label so the status reads like an SOP, not a log tail.
    function phaseOf(msg) {
      var m = (msg || "").toLowerCase();
      if (m.indexOf("connecting") !== -1) return "Connecting to the board…";
      if (m.indexOf("chip is") !== -1 || m.indexOf("detecting chip") !== -1) return "Board detected — preparing…";
      if (m.indexOf("erasing") !== -1 || m.indexOf("erase") !== -1) return "Erasing flash…";
      if (m.indexOf("writing") !== -1 || m.indexOf("wrote ") !== -1) return "Writing firmware…";
      if (m.indexOf("hash of data verified") !== -1 || m.indexOf("verify") !== -1) return "Verifying…";
      if (m.indexOf("hard resetting") !== -1 || m.indexOf("leaving") !== -1) return "Finishing up…";
      return null;   // keep the current phase for uninteresting lines
    }
    function setStatusLine(text, cls) {
      if (!statusEl) return;
      statusEl.textContent = text;
      statusEl.style.color = cls === "ok" ? "var(--green)" : cls === "er" ? "var(--red)" : "var(--tx)";
    }

    document.querySelectorAll(".fw-flash").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (!fwPort) { if (portMsg) { portMsg.textContent = "click a target port first"; portMsg.style.color = "var(--red)"; } return; }
        var profile = btn.dataset.profile;
        var vsel = document.querySelector('.fw-variant[data-profile="' + profile + '"]');
        var variant = vsel ? vsel.value : "";
        if (!window.confirm("Flash " + profile + " to " + fwPort + "?\n\nThis overwrites the device firmware and cannot be undone mid-write.")) return;
        if (bar) bar.style.width = "0";
        if (pctEl) pctEl.textContent = "";
        if (bootCallout) bootCallout.style.display = "none";
        if (logEl) { logEl.innerHTML = ""; appendLine(logEl, "p", "[Flashing " + profile + " → " + fwPort + "]"); }
        setStatusLine("Starting…", "");
        sawProgress = false;
        ensureSocket();
        postJSON("/api/flash", { port: fwPort, profile_id: profile, variant: variant })
          .then(function () {
            appendLine(logEl, "tx", "flash started…");
            // Watchdog: if the live progress stream says nothing for a while, the socket may be down —
            // tell the user instead of leaving them at a silent bar (Ace's "held BOOT, nothing popped up").
            if (flashWatchdog) clearTimeout(flashWatchdog);
            flashWatchdog = setTimeout(function () {
              if (!sawProgress) {
                setStatusLine("No response yet — the board may not be in download mode.", "er");
                if (bootCallout) bootCallout.style.display = "";
                appendLine(logEl, "er", "no progress from the flasher yet — hold BOOT and try again, or reconnect the cable.");
              }
            }, 12000);
          })
          .catch(function (err) { setStatusLine("Couldn't start the flash", "er"); appendLine(logEl, "er", "error: " + err); });
      });
    });

    // flash progress stream
    ensureSocket();
    if (socket) socket.on("flash_progress", function (d) {
      if (!d || (fwPort && d.port && d.port !== fwPort)) return;
      sawProgress = true;
      if (flashWatchdog) { clearTimeout(flashWatchdog); flashWatchdog = null; }
      if (bar && typeof d.percent === "number") bar.style.width = d.percent + "%";
      if (pctEl && typeof d.percent === "number" && !d.done) pctEl.textContent = d.percent + "%";
      if (d.message) {
        appendLine(logEl, d.done ? (d.success ? "ok" : "er") : "rx", d.message);
        if (!d.done) { var ph = phaseOf(d.message); if (ph) setStatusLine(ph, ""); }
        if (!d.done && isDownloadModeFail(d.message) && bootCallout) bootCallout.style.display = "";
      }
      if (d.done) {
        if (pctEl) pctEl.textContent = d.success ? "100%" : "";
        setStatusLine(d.success ? "✔ Flashed + verified" : "✖ Flash failed", d.success ? "ok" : "er");
        if (!d.success && bootCallout && isDownloadModeFail(d.message)) bootCallout.style.display = "";
        notifyDesktop(d.success ? "Flash complete" : "Flash failed",
          (d.port || "device") + (d.message ? " — " + d.message : ""));
      }
    });
  }
  initFirmware();

  // Remember each collapsible card's open/closed state per viewer (localStorage), so a terminal the user
  // expanded stays expanded across reloads and one they collapsed stays out of the way. Best-effort — a
  // private window / blocked storage just falls back to the template default (collapsed).
  (function persistCollapsibles() {
    document.querySelectorAll("details.collapsible[id]").forEach(function (d) {
      var key = "cc.collapse." + d.id;
      try {
        var saved = localStorage.getItem(key);
        if (saved === "1") d.open = true;
        else if (saved === "0") d.open = false;
      } catch (e) { /* storage unavailable */ }
      d.addEventListener("toggle", function () {
        try { localStorage.setItem(key, d.open ? "1" : "0"); } catch (e) { /* ignore */ }
      });
    });
  })();

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
        if (!confirmIfDangerous(cmd)) return;
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

  // ── SETTINGS: live read + owner write-back to the real settings store ─────────
  // Hydrates every control from /api/settings, then Save posts the whole form back (secret-free:
  // the WiGLE token only leaves the box when the user types a NEW one). Gate status stays read-only.
  function chipOn(el) { return !!(el && el.classList.contains("on")); }
  function setChip(el, on) {
    if (!el) return;
    el.classList.toggle("on", !!on);
    el.setAttribute("aria-checked", on ? "true" : "false");
    var t = el.textContent.replace(/^[☐☑☒]︎?\s*/, "");
    el.textContent = (on ? "☑︎ " : "☐︎ ") + t;
  }
  function setSelect(el, val) {
    if (!el) return;
    var v = String(val);
    for (var i = 0; i < el.options.length; i++) {
      if ((el.options[i].value || el.options[i].text) === v) { el.selectedIndex = i; return; }
    }
  }

  function initSettings() {
    var gate = document.getElementById("set-gate");
    if (!gate) return;   // not the reform SETTINGS view

    // Access gate — read-only live status (never a secret).
    getJSON("/api/gate-status").then(function (g) {
      var factors = [];
      if (g.has_password) factors.push("password");
      if (g.has_key) factors.push("USB key");
      gate.textContent = "configured=" + g.configured + " · policy=" + g.policy +
        " · factors=" + (factors.join("+") || "none") +
        (g.locked ? " · LOCKED (" + g.remaining_secs + "s)" : "");
    }).catch(function () { gate.textContent = "gate status unavailable"; });

    // Remote access — reveal the web credentials + LAN URL to this already-authenticated operator so they
    // can open the UI from a phone / another PC. Only a generated one-time password comes back; a user-set
    // CC_WEB_PASS is never echoed (the server returns password:null, generated:false).
    var remote = document.getElementById("set-remote");
    function loadRemote() {
      if (!remote) return;
      getJSON("/api/remote-access").then(function (r) {
        var url = (r.lan_ip && r.port) ? ("http://" + r.lan_ip + ":" + r.port + "/reform")
                                       : "http://<this-PC's-LAN-IP>:<port>/reform";
        var pw;
        if (r.source === "saved") pw = "(the password you set below)";
        else if (r.source === "env") pw = "(set via the CC_WEB_PASS environment variable)";
        else if (r.revealed) pw = r.password;
        else pw = "(one-time — shown only on the host PC's console; set your own below)";
        remote.innerHTML =
          "URL&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<b>" + esc(url) + "</b><br>" +
          "user&nbsp;&nbsp;&nbsp;&nbsp;" + esc(r.username || "admin") + "<br>" +
          "password&nbsp;" + esc(pw);
        var chip = document.getElementById("pw-source-chip");
        if (chip) {
          var label = { saved: "saved on this PC", env: "from CC_WEB_PASS", generated: "one-time" }[r.source] || r.source;
          chip.textContent = label;
          chip.className = "chip" + (r.source === "saved" ? " on" : "");
        }
        var uf = document.getElementById("pw-user");
        if (uf && !uf.value) uf.value = r.username || "admin";
      }).catch(function () { remote.textContent = "remote-access info unavailable"; });
    }
    loadRemote();
    (function wirePassword() {
      var saveBtn = document.getElementById("pw-save");
      var resetBtn = document.getElementById("pw-reset");
      var status = document.getElementById("pw-status");
      function say(msg, isErr) { if (status) { status.textContent = msg; status.style.color = isErr ? "var(--red)" : "var(--dim)"; } }
      if (saveBtn) saveBtn.addEventListener("click", function () {
        var np = (document.getElementById("pw-new").value || "");
        var un = (document.getElementById("pw-user").value || "").trim();
        if (np.length < 8) { say("Password must be at least 8 characters.", true); return; }
        saveBtn.disabled = true;
        postJSON("/api/web-password", { new_password: np, username: un }).then(function (d) {
          document.getElementById("pw-new").value = "";
          say("Password saved. Use it next time you log in (this session stays open).");
          loadRemote();
          recoverSocketAfterAuthChange();   // our cookie is re-stamped; reconnect our revoked live socket
        }).catch(function (err) {
          say("Could not save: " + (typeof err === "string" ? err : "error"), true);
        }).then(function () { saveBtn.disabled = false; });
      });
      if (resetBtn) resetBtn.addEventListener("click", function () {
        if (!window.confirm("Forget the saved password? CC will fall back to CC_WEB_PASS or a one-time password on the next start.")) return;
        postJSON("/api/web-password", { reset: true }).then(function (d) {
          say(d.note || "Saved password cleared.");
          loadRemote();
          recoverSocketAfterAuthChange();   // our cookie is re-stamped; reconnect our revoked live socket
        }).catch(function (err) {
          say("Could not reset: " + (typeof err === "string" ? err : "error"), true);
        });
      });
    })();

    // Every chip is a keyboard-operable switch (WCAG 2.1.1/4.1.2); click OR Enter/Space toggles it,
    // and Save reads its state.
    ["set-updates-enabled", "set-confirm-dangerous",
     "set-suppress-warnings", "set-secure-container"].forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      el.setAttribute("role", "switch");
      el.setAttribute("tabindex", "0");
      el.setAttribute("aria-checked", chipOn(el) ? "true" : "false");
      function toggleChip() { setChip(el, !chipOn(el)); }
      el.addEventListener("click", toggleChip);
      el.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleChip(); }
      });
    });

    var statusEl = document.getElementById("set-status");
    function setStatus(t, err) {
      if (!statusEl) return;
      statusEl.textContent = t || "";
      statusEl.style.color = err ? "var(--red)" : "var(--dim)";
    }

    // Hydrate from the real store.
    function hydrate(s) {
      if (!s) return;
      setSelect(document.getElementById("set-serial-baud"), s.serial.default_baud);
      // null/absent flash_baud == "Auto" (use the firmware's own baud); a number is an explicit override.
      setSelect(document.getElementById("set-flash-baud"), s.flash.flash_baud == null ? "auto" : s.flash.flash_baud);
      setSelect(document.getElementById("set-touch-mode"), s.interface.touch_mode);
      setChip(document.getElementById("set-updates-enabled"), s.updates.enabled);
      setChip(document.getElementById("set-confirm-dangerous"), s.safety.confirm_dangerous);
      setChip(document.getElementById("set-suppress-warnings"), s.safety.suppress_all_warnings);
      setChip(document.getElementById("set-secure-container"), s.security.secure_container);
      var vd = document.getElementById("set-vault-dir");
      if (vd) vd.value = s.vault.dir;
      var wt = document.getElementById("set-wigle-state");
      if (wt) wt.textContent = s.uploads.wigle_token_set ? "· set" : "· not set";
      var wi = document.getElementById("set-wigle-token");
      if (wi) wi.value = "";   // never echo the token back; blank = "leave unchanged"
    }
    getJSON("/api/settings").then(function (r) { hydrate(r.settings); })
      .catch(function () { setStatus("could not load settings", true); });

    // Gather the form into the /api/settings shape.
    function gather() {
      var tok = document.getElementById("set-wigle-token");
      var body = {
        serial: { default_baud: parseInt(valOf("set-serial-baud"), 10) },
        flash: {
          // "auto" -> null (use the firmware's own baud); a number -> explicit override.
          flash_baud: valOf("set-flash-baud") === "auto" ? null : parseInt(valOf("set-flash-baud"), 10),
        },
        interface: { touch_mode: valOf("set-touch-mode") },
        updates: { enabled: chipOn(document.getElementById("set-updates-enabled")) },
        safety: {
          confirm_dangerous: chipOn(document.getElementById("set-confirm-dangerous")),
          suppress_all_warnings: chipOn(document.getElementById("set-suppress-warnings")),
        },
        security: { secure_container: chipOn(document.getElementById("set-secure-container")) },
        vault: { dir: valOf("set-vault-dir") },
      };
      // Only send the token when the user typed a new one (blank field = leave the stored one alone).
      if (tok && tok.value.trim()) body.uploads = { wigle_token: tok.value.trim() };
      return body;
    }
    function valOf(id) { var el = document.getElementById(id); return el ? (el.value || "") : ""; }

    var saveBtn = document.getElementById("set-save");
    if (saveBtn) saveBtn.addEventListener("click", function () {
      setStatus("saving…");
      postJSON("/api/settings", gather()).then(function (r) {
        hydrate(r.settings);
        setStatus("saved ✓");
      }).catch(function () { setStatus("save failed — check the values", true); });
    });

    var resetBtn = document.getElementById("set-reset");
    if (resetBtn) resetBtn.addEventListener("click", function () {
      if (!window.confirm("Reset all settings to defaults?")) return;
      setStatus("resetting…");
      postJSON("/api/settings", { reset: true }).then(function (r) {
        hydrate(r.settings);
        setStatus("reset to defaults ✓");
      }).catch(function () { setStatus("reset failed", true); });
    });

    // Updates card — real current version + a live "Check now".
    var updStatus = document.getElementById("set-update-status");
    getJSON("/api/version").then(function (v) {
      if (updStatus) updStatus.textContent = "Current v" + v.version;
    }).catch(function () { if (updStatus) updStatus.textContent = "version unavailable"; });

    var checkBtn = document.getElementById("set-check-now");
    if (checkBtn) checkBtn.addEventListener("click", function () {
      if (updStatus) updStatus.textContent = "checking…";
      postJSON("/api/updates/check", {}).then(function (r) {
        if (!updStatus) return;
        if (r.status === "NEWER") {
          updStatus.innerHTML = "v" + esc(r.current) + " · update available: " +
            '<a href="' + esc(r.latest_url) + '" target="_blank" rel="noopener">' +
            esc(r.latest_tag) + "</a>";
        } else if (r.status === "OFFLINE") {
          updStatus.textContent = "v" + r.current + " · offline (couldn’t reach GitHub)";
        } else {
          updStatus.textContent = "v" + r.current + " · up to date";
        }
      }).catch(function () { if (updStatus) updStatus.textContent = "check failed"; });
    });

    // "Set up access gate…" — the gate is configured from the desktop app/console by design.
    var gateBtn = document.getElementById("set-gate-setup");
    if (gateBtn) gateBtn.addEventListener("click", function () {
      setStatus("The access gate is set up in the desktop app / console (Security ▸ Access Gate) — " +
        "it never takes a password over the web.", false);
    });
  }
  initSettings();

  // OPERATE Macros card — real saved macros from /api/macros (list only; run is device-gated).
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
      body.innerHTML = ms.length ? "" : '<tr><td class="off" colspan="4">no saved macros yet — this build plays back saved macros; recording isn\'t exposed in the web UI yet</td></tr>';
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
      s.on("macro_done", function (d) {
        d = d || {};
        setStatus(d.success ? "✓ " + (d.message || "done") : "✗ " + (d.message || "failed"), !d.success);
        notifyDesktop(d.success ? "Macro finished" : "Macro stopped",
          (d.macro || "macro") + (d.message ? " — " + d.message : ""));
      });
    }
  }
  initMacros();

  // OPERATE ▸ Broadcast — one command → many checked devices. Offensive
  // commands are gated by the "authorized use" chip + a server-side re-check; recon fans out freely.
  function initBroadcast() {
    var list = document.getElementById("bc-devices");
    var input = document.getElementById("bc-input");
    var sendBtn = document.getElementById("bc-send");
    var consent = document.getElementById("bc-consent");
    var statusEl = document.getElementById("bc-status");
    var resultsEl = document.getElementById("bc-results");
    if (!list || !sendBtn) return;

    function setStatus(t, err) {
      if (!statusEl) return;
      statusEl.textContent = t || "";
      statusEl.style.color = err ? "var(--red)" : "var(--dim)";
    }

    // Re-render the device checklist, preserving which ports were already checked.
    window.__bcSyncDevices = function (devs) {
      var checked = {};
      list.querySelectorAll('input[type="checkbox"]:checked').forEach(function (cb) {
        checked[cb.value] = true;
      });
      var connected = (devs || []).filter(function (d) { return d.connected; });
      if (!connected.length) {
        list.innerHTML = '<span class="dim" style="font-size:12px">no connected devices — connect some in DEVICE ▸ Dashboard</span>';
        return;
      }
      list.innerHTML = connected.map(function (d) {
        var on = checked[d.port] ? " checked" : "";
        return '<label class="chip"><input type="checkbox" value="' + esc(d.port) + '"' + on +
          ' style="vertical-align:-1px"> ' + esc(d.port) + " — " +
          esc(d.firmware || d.name || "device") + "</label>";
      }).join("");
    };

    function selectedPorts() {
      return Array.prototype.map.call(
        list.querySelectorAll('input[type="checkbox"]:checked'),
        function (cb) { return cb.value; }
      );
    }

    sendBtn.addEventListener("click", function () {
      var cmd = (input.value || "").trim();
      var ports = selectedPorts();
      if (!cmd) { setStatus("type a command to broadcast", true); return; }
      if (!ports.length) { setStatus("check at least one device", true); return; }
      if (!window.confirm("Broadcast to " + ports.length + " device(s):\n\n" + cmd + "\n\nProceed?")) return;
      setStatus("broadcasting to " + ports.length + " device(s)…");
      if (resultsEl) resultsEl.innerHTML = "";
      postJSON("/api/broadcast", { command: cmd, ports: ports, consent: !!(consent && consent.checked) })
        .then(function (r) {
          setStatus("sent to " + r.sent + " · failed " + r.failed +
            (r.offensive ? " · flagged transmitting" : ""));
          if (resultsEl) {
            resultsEl.innerHTML = (r.results || []).map(function (x) {
              var ok = x.status === "sent";
              return "<tr><td class='mono'>" + esc(x.port) + "</td><td class='" +
                (ok ? "con" : "off") + "'>" + esc(ok ? "sent" : (x.error || "failed")) + "</td></tr>";
            }).join("");
          }
        })
        .catch(function (err) {
          // postJSON rejects with the server's `error` string (e.g. the offensive-gate 403), else a status.
          setStatus(typeof err === "string" ? err : "broadcast refused (" + err + ")", true);
        });
    });
    if (input) input.addEventListener("keydown", function (e) { if (e.key === "Enter") sendBtn.click(); });
  }
  initBroadcast();

  // Cross-Comm live event stream (stream half) — read-only fan-out of the bus events the app
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

  // Cross-Comm auto-routing rules (rules half) — offensive rules are consent-gated on add AND
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
      if (desc && img) desc.textContent = (img.description || "") + " — SHA-256 verified (plus OpenPGP when the signing key is available) before writing.";
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

  // HUNT Tail Detect — live follower/persistence table from /api/tails (fed by the pool's BLE +
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
    var packs = panel.__packs || [];
    var dfn = panel.__defender || {};
    var html = '<div class="dim" style="font-size:11px;margin-bottom:4px">Optional engines. The built-in native cracker needs none of these, and everything here is bundled (no download).</div>';
    // Windows Defender flags these standard tools as PUA — offer the one-time folder exclusion up front.
    if (dfn.is_windows && dfn.pua_on) {
      html +=
        '<div class="dim" style="font-size:11px;margin:4px 0">Windows Defender flags these standard tools as PUA and deletes them unless you add a one-time exclusion for CC\'s tools folder (that folder only). Prefer not to? The native cracker already does the job.</div>' +
        '<div class="row" style="align-items:center;gap:6px;margin:4px 0">' +
        '<input class="field mono" id="crack-excl-cmd" readonly style="flex:1;font-size:10.5px" value="' + esc(dfn.exclusion_command || "") + '">' +
        '<button class="btn sm" id="crack-excl-copy">Copy</button>' +
        '<button class="btn sm" id="crack-excl-add">Add exclusion (admin)</button></div>';
    }
    // Bundled packs — Enable unpacks the encrypted pack (offline). The design path, no vendor fetch.
    packs.forEach(function (p) {
      html += '<div class="row" style="align-items:center;margin:4px 0;font-size:11px">' +
        '<span style="flex:1"><b>' + esc(p.tool) + '</b> ' + esc(p.version) + ' &#8212; bundled, encrypted, no download. Enable to unpack it and use its full CLI.</span>' +
        '<button class="btn sm" data-enable="' + esc(p.name) + '">Enable</button></div>';
    });
    // Detected tools + guidance for anything without a bundled pack.
    avail.forEach(function (a) {
      var hasPack = packs.some(function (p) { return p.tool === a.tool; });
      if (a.present) {
        html += '<div class="row" style="font-size:11px;margin:3px 0"><span style="color:var(--green)">&#10003; ' + esc(a.tool) + ' detected' + (a.source ? " (" + esc(a.source) + ")" : "") + ' &#8212; usable as-is</span></div>';
      } else if (!hasPack) {
        html += crackToolRow(a);
      }
    });
    html += '<div id="crack-panel-msg" class="dim" style="font-size:11px;margin-top:6px"></div>';
    panel.innerHTML = html;
    var msg = panel.querySelector("#crack-panel-msg");
    function setMsg(color, text) { if (msg) { msg.style.color = color; msg.textContent = text; } }

    // Enable a bundled pack (offline, no network).
    panel.querySelectorAll("button[data-enable]").forEach(function (b) {
      b.addEventListener("click", function () {
        var name = b.getAttribute("data-enable");
        b.disabled = true; b.textContent = "Enabling...";
        setMsg("var(--dim)", "Unpacking " + name + "...");
        postJSON("/api/crack/enable-bundled", { pack: name }).then(function (res) {
          b.textContent = res.ok ? "Enabled" : "Enable";
          b.disabled = !!res.ok;
          setMsg(res.ok ? "var(--green)" : "var(--amber)", (res.ok ? "✓ " : "✗ ") + (res.message || (res.ok ? "enabled" : "enable failed")));
          if (res.ok) initCrack();
        }).catch(function (err) {
          b.disabled = false; b.textContent = "Enable";
          setMsg("var(--amber)", "✗ " + ((err && err.message) || (typeof err === "string" ? err : "enable failed")));
        });
      });
    });

    // Defender exclusion: copy the command, or one-click elevated add.
    var copyBtn = panel.querySelector("#crack-excl-copy");
    if (copyBtn) copyBtn.addEventListener("click", function () {
      var f = panel.querySelector("#crack-excl-cmd");
      if (f) { f.select(); try { document.execCommand("copy"); } catch (e) {} }
      setMsg("var(--dim)", "exclusion command copied");
    });
    var addBtn = panel.querySelector("#crack-excl-add");
    if (addBtn) addBtn.addEventListener("click", function () {
      addBtn.disabled = true;
      setMsg("var(--dim)", "Requesting an elevated exclusion - approve the UAC prompt...");
      postJSON("/api/crack/defender-exclusion", {}).then(function (res) {
        addBtn.disabled = false;
        setMsg(res.ok ? "var(--green)" : "var(--amber)", res.ok ? "✓ exclusion added - now Enable a tool" : "not added (declined/failed) - run the command manually");
      }).catch(function () { addBtn.disabled = false; setMsg("var(--amber)", "exclusion request failed - run the command manually"); });
    });

    // Fallback: network install only for a tool with no bundled pack that CC can still auto-fetch.
    panel.querySelectorAll("button[data-install]").forEach(function (b) {
      b.addEventListener("click", function () {
        var tool = b.getAttribute("data-install");
        b.disabled = true; b.textContent = "Installing...";
        setMsg("var(--dim)", "Fetching " + tool + "...");
        postJSON("/api/crack/install-tool", { tool: tool }).then(function (res) {
          setMsg("var(--green)", "✓ " + tool + " installed → " + (res.path || "tools dir"));
          initCrack();
        }).catch(function (err) {
          b.disabled = false; b.textContent = "Install";
          setMsg("var(--amber)", "✗ " + ((err && err.message) || (typeof err === "string" ? err : "install failed")));
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
      eng.innerHTML = backends.map(function (b) { return '<option value="' + esc(b) + '">' + esc(labels[b] || b) + "</option>"; }).join("");
      foot.innerHTML = (d.tools || []).map(function (t) {
        var mark = t.present ? "✓" : "✗";
        var col = t.present ? "var(--green)" : "var(--dim)";
        return '<span style="color:' + col + '">' + mark + " " + esc(t.name) + (t.present && t.version ? " " + esc(t.version.split(" ")[0]) : "") + "</span>";
      }).join(" · ") || "native ready";
      var panel = document.getElementById("crack-tools-panel");
      if (panel) { panel.__avail = d.availability || []; panel.__packs = d.packs || []; panel.__defender = d.defender || {}; if (!panel.hidden) renderCrackPanel(panel); }
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

  // ── DEVICE ▸ Flipper Zero: qFlipper provisioning + device-management ops ──
  function flipperLog(text, isErr) {
    var pre = document.getElementById("flipper-log");
    if (!pre) return;
    pre.style.display = "block";
    pre.textContent = text || "";
    pre.style.color = isErr ? "var(--red)" : "";
  }
  function renderFlipper(d) {
    var body = document.getElementById("flipper-body");
    var status = document.getElementById("flipper-status");
    if (!body) return;
    if (status) status.textContent = d.present ? ("qFlipper " + (d.source || "ready")) : "qFlipper not installed";
    var html = "";
    if (!d.present) {
      html += '<div class="card2">CC drives the headless qFlipper to flash Flipper firmware and manage the device. It is not bundled into the installer — install it once and CC keeps it in its tools folder.</div>';
      if (d.can_provision) {
        html += '<button class="btn acc" data-flip="install" style="margin-top:8px">Get qFlipper</button>';
      } else {
        html += '<div class="card2" style="margin-top:8px">On this OS, install qFlipper from flipperzero.one/update — CC will then find it on PATH.</div>';
      }
    } else {
      html += '<div class="kv mono muted">cli: ' + esc(d.cli || "") + "</div>";
      html += '<div class="row" style="margin-top:8px;gap:6px;flex-wrap:wrap">';
      (d.control_ops || []).forEach(function (op) {
        var cls = op.destructive ? "btn danger" : "btn";
        html += '<button class="' + cls + '" data-flip="op" data-op="' + esc(op.op) +
          '" data-destructive="' + (op.destructive ? 1 : 0) + '" title="' + esc(op.help) + '">' + esc(op.label) + "</button>";
      });
      html += "</div>";
      html += '<div class="card2" style="margin-top:6px">qFlipper-cli finds the Flipper over USB — no COM port needed. Live serial control (SubGHz / NFC / RFID / IR / GPIO) is under Operate &amp; Terminal.</div>';
    }
    body.innerHTML = html;
  }
  function initFlipper() {
    if (!document.getElementById("flipper-body")) return;
    getJSON("/api/qflipper").then(renderFlipper).catch(function () {
      var b = document.getElementById("flipper-body");
      if (b) b.textContent = "qFlipper status unavailable";
    });
  }
  (function wireFlipper() {
    var card = document.getElementById("flipper-card");
    if (!card) return;
    card.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-flip]");
      if (!btn) return;
      var kind = btn.getAttribute("data-flip");
      if (kind === "install") {
        e.preventDefault();
        btn.disabled = true;
        btn.textContent = "Downloading qFlipper…";
        flipperLog("Downloading + verifying qFlipper (~65 MB)… this runs once.");
        postJSON("/api/qflipper/install", {}).then(function (d) {
          flipperLog((d.log || []).join("\n") + "\nqFlipper ready.");
          initFlipper();
        }).catch(function (err) {
          flipperLog("Install failed: " + (typeof err === "string" ? err : JSON.stringify(err)), true);
          btn.disabled = false;
          btn.textContent = "Get qFlipper";
        });
      } else if (kind === "op") {
        e.preventDefault();
        var op = btn.getAttribute("data-op");
        var destructive = btn.getAttribute("data-destructive") === "1";
        var reqBody = { op: op };
        if (op === "backup" || op === "restore") {
          var p = window.prompt(op === "backup" ? "Backup the Flipper's internal storage to which folder on this PC?" : "Restore from which folder on this PC?", "");
          if (!p) return;
          reqBody.path = p;
        }
        if (destructive || op === "update") {
          var warn = btn.getAttribute("title") || "This affects the Flipper.";
          if (!window.confirm(warn + "\n\nProceed?")) return;
          reqBody.confirm = true;
        }
        flipperLog("Running " + op + " … (watch the Flipper screen for prompts)");
        postJSON("/api/qflipper/control", reqBody).then(function (d) {
          flipperLog((d.log || []).join("\n") + "\n" + (d.ok ? (op + " complete.") : (op + " failed (rc " + d.rc + ").")), !d.ok);
        }).catch(function (err) {
          flipperLog(op + " failed: " + (typeof err === "string" ? err : JSON.stringify(err)), true);
        });
      }
    });
    initFlipper();
  })();

  // ── OPERATE ▸ Antenna: wavelength + element lengths from a frequency (pure math) ──
  (function wireAntenna() {
    var btn = document.getElementById("ant-calc");
    if (!btn) return;
    var out = document.getElementById("ant-result");
    function calc() {
      var freq = (document.getElementById("ant-freq").value || "").trim();
      var unit = document.getElementById("ant-unit").value;
      var vf = (document.getElementById("ant-vf").value || "1.0").trim();
      if (!freq) { out.innerHTML = '<div class="card2">Enter a frequency first.</div>'; return; }
      var q = "/api/antenna?freq=" + encodeURIComponent(freq) + "&unit=" + encodeURIComponent(unit) + "&vf=" + encodeURIComponent(vf);
      out.innerHTML = '<div class="card2">calculating…</div>';
      getJSON(q).then(function (d) {
        var band = d.band ? " · " + esc(d.band) : "";
        var wl = d.wavelength;
        var rows = Object.keys(d.elements).map(function (k) {
          var e = d.elements[k];
          return "<tr><td>" + esc(e.label) + '</td><td class="mono r">' + e.cm + ' cm</td><td class="mono r">' + e.in + ' in</td><td class="mono r">' + e.mm + " mm</td></tr>";
        }).join("");
        var notes = (d.notes || []).map(function (n) { return "<li>" + esc(n) + "</li>"; }).join("");
        out.innerHTML =
          '<div class="kv mono" style="margin-bottom:6px">' + esc(d.freq_label) + band + " · λ = " + wl.cm + " cm / " + wl.in + " in" + (d.velocity_factor !== 1 ? (" · VF " + d.velocity_factor) : "") + "</div>" +
          '<table><thead><tr><th>Element</th><th class="r">metric</th><th class="r">inch</th><th class="r">mm</th></tr></thead><tbody>' + rows + "</tbody></table>" +
          (notes ? ('<ul class="footnote" style="margin-top:8px;padding-left:16px">' + notes + "</ul>") : "");
      }).catch(function (err) {
        out.innerHTML = '<div class="card2" style="color:var(--red)">' + esc(typeof err === "string" ? err : "calculation failed") + "</div>";
      });
    }
    btn.addEventListener("click", calc);
    var f = document.getElementById("ant-freq");
    if (f) f.addEventListener("keydown", function (e) { if (e.key === "Enter") calc(); });
  })();

  // ── DEVICE ▸ Orbic / rayhunter (IMSI-catcher / cell-site-simulator detector) ──
  function orbicLog(text, isErr) {
    var p = document.getElementById("orbic-log");
    if (!p) return;
    p.style.display = "block";
    p.textContent = text || "";
    p.style.color = isErr ? "var(--red)" : "";
  }
  function renderOrbic(d) {
    var body = document.getElementById("orbic-body");
    var status = document.getElementById("orbic-status");
    if (!body) return;
    var st = d.status || {}, cf = d.conflict || {};
    if (status) status.textContent = st.running ? ("running v" + (st.version || "?")) : (st.reachable ? "reachable" : "IMSI-catcher detector");
    var html = "";
    if (st.running) {
      html += '<div class="kv mono" style="color:var(--green)">rayhunter ' + esc(st.version || "") + ' running · <a href="' + esc(st.url) + '" target="_blank" rel="noopener" style="color:var(--acc)">open dashboard</a> · <button class="btn sm" data-orbic="snapshot">Live view</button></div>';
      html += '<div id="orbic-snapshot" class="footnote" style="margin-top:4px"></div>';
    } else {
      html += '<div class="card2">rayhunter is a headless daemon on the Orbic RC400L (detects IMSI catchers / cell-site simulators). It installs over the network and needs a <b>deactivated SIM</b> in the Orbic to actually capture. Nothing shows on the Orbic\'s own screen.</div>';
    }
    if (cf.conflict) {
      html += '<div class="footnote" style="color:var(--amber);margin-top:6px">Heads up: another network (' + esc((cf.other_ifaces || []).join(", ")) + ") shares the Orbic's 192.168.1.x subnet, so this PC can't reliably reach it. Open the dashboard from a phone on the Orbic's WiFi, or disconnect that adapter.</div>";
    }
    html += '<div class="row" style="margin-top:8px;gap:6px;flex-wrap:wrap;align-items:center">' +
      '<input class="field mono" id="orbic-pw" type="password" aria-label="Orbic admin password" placeholder="Orbic admin / WiFi password" style="width:200px">' +
      '<button class="btn acc" id="orbic-install">Install rayhunter</button></div>' +
      '<div class="footnote" style="margin-top:4px">Installer: ' + (d.installer_present ? "ready" : "downloads on first use") + ".</div>";
    body.innerHTML = html;
  }
  function initOrbic() {
    if (!document.getElementById("orbic-body")) return;
    getJSON("/api/rayhunter").then(renderOrbic).catch(function () {
      var b = document.getElementById("orbic-body");
      if (b) b.textContent = "rayhunter status unavailable";
    });
  }
  (function wireOrbic() {
    var card = document.getElementById("orbic-card");
    if (!card) return;
    card.addEventListener("click", function (e) {
      var snap = e.target.closest('[data-orbic="snapshot"]');
      if (snap) {
        e.preventDefault();
        var out = document.getElementById("orbic-snapshot");
        if (out) out.textContent = "reading live status…";
        getJSON("/api/rayhunter/snapshot").then(function (d) {
          if (!out) return;
          var rec = d.recording === "recording" ? ("recording (" + esc(d.recording_name || "?") + ")")
            : (d.recording === "stopped" ? "not recording" : "recording state unknown");
          var batt = d.battery && d.battery.level != null ? (" · battery " + esc(String(d.battery.level))) : "";
          var actions = d.recording_name ?
            (' · <button class="btn sm" data-orbic="report" data-name="' + esc(d.recording_name) + '">Report</button>' +
             ' <button class="btn sm" data-orbic="export" data-name="' + esc(d.recording_name) + '">Export HTML</button>') : "";
          out.innerHTML = "transport " + esc(d.transport) + " · " + rec + batt + actions +
            '<br><span style="color:var(--dim)">Read-only. A reachable daemon isn\'t proof of capture — needs a SIM + live traffic. Events are heuristic, not proof of an IMSI catcher.</span>' +
            '<div id="orbic-report"></div>';
        }).catch(function () { if (out) out.textContent = "live status unavailable"; });
        return;
      }
      var rep = e.target.closest('[data-orbic="report"]');
      if (rep) {
        e.preventDefault();
        var rout = document.getElementById("orbic-report");
        if (rout) rout.textContent = "loading report…";
        getJSON("/api/rayhunter/report?name=" + encodeURIComponent(rep.getAttribute("data-name"))).then(function (r) {
          if (!rout) return;
          var c = r.counts || {}, by = c.by_level || {};
          var cov = r.complete ? "" : (' · <span style="color:var(--amber)">incomplete (' + esc(r.coverage) + ')</span>');
          rout.innerHTML = 'warnings ' + esc(String(c.warnings)) + ' (H ' + esc(String(by.High)) + '/M ' +
            esc(String(by.Medium)) + '/L ' + esc(String(by.Low)) + ') · info ' + esc(String(c.informational)) +
            ' · skipped ' + esc(String(c.skipped)) + cov;
        }).catch(function () { if (rout) rout.textContent = "report unavailable"; });
        return;
      }
      var exp = e.target.closest('[data-orbic="export"]');
      if (exp) {
        e.preventDefault();
        getJSON("/api/rayhunter/export?name=" + encodeURIComponent(exp.getAttribute("data-name"))).then(function (x) {
          if (!x.html) return;
          // The export HTML is fully escaped server-side; render it via a Blob URL (no document.write).
          var url = URL.createObjectURL(new Blob([x.html], { type: "text/html" }));
          window.open(url, "_blank");
          setTimeout(function () { URL.revokeObjectURL(url); }, 30000);
        }).catch(function () { });
        return;
      }
      var btn = e.target.closest("#orbic-install");
      if (!btn) return;
      e.preventDefault();
      var pwEl = document.getElementById("orbic-pw");
      var pw = pwEl ? pwEl.value : "";
      if (!pw) { orbicLog("Enter the Orbic's admin / WiFi password first.", true); return; }
      if (!window.confirm("Install rayhunter on the Orbic? This modifies the hotspot (installs a daemon and reboots it).")) return;
      btn.disabled = true;
      btn.textContent = "Installing…";
      orbicLog("Installing rayhunter over the network… (~1 min, the Orbic reboots at the end)");
      postJSON("/api/rayhunter/install", { admin_password: pw }).then(function (r) {
        orbicLog((r.log || []).join("\n") + "\n" + (r.ok ? "Install complete." : "Install failed (rc " + r.rc + ")."), !r.ok);
        if (pwEl) pwEl.value = "";
        initOrbic();
      }).catch(function (err) {
        orbicLog("Install failed: " + (typeof err === "string" ? err : JSON.stringify(err)), true);
        btn.disabled = false;
        btn.textContent = "Install rayhunter";
      });
    });
    initOrbic();
  })();

  // ── CRACK ▸ Wordlists: bundled + installed picker, on-demand catalog download, bring-your-own ─
  function initWordlists() {
    var sel = document.getElementById("wl-select");
    if (!sel) return;
    var panel = document.getElementById("wl-catalog-panel");
    var foot = document.getElementById("wl-foot");
    var byoEntries = [];   // user-added bring-your-own wordlists, preserved across catalog refreshes (F05)
    function wlOption(w, group) {
      var size = w.size_human ? " (" + esc(w.size_human) + ")" : "";
      return '<option value="' + esc(w.path || w.name) + '">' + esc(w.name) + size + (group ? " · " + group : "") + "</option>";
    }
    function load() {
      var prev = sel.value;
      // Revalidate the tracked BYO paths on the server FIRST: a BYO file deleted or made unreadable after
      // it was added must drop out of the picker (and, if it was selected, surface an unavailable notice)
      // rather than keep showing as selectable and be cracked against silently (N04). We keep them tracked
      // in byoEntries but only re-insert the ones that still validate; a network hiccup on the check is
      // treated as "still there" so a transient blip doesn't wipe the user's lists.
      var byoCheck = byoEntries.length
        ? postJSON("/api/wordlists/byo/validate", { paths: byoEntries.map(function (e) { return e.path; }) })
            .then(function (r) { return (r && r.valid) || null; })
            .catch(function () { return null; })
        : Promise.resolve([]);
      Promise.all([getJSON("/api/wordlists"), byoCheck]).then(function (res) {
        var d = res[0];
        var validPaths = res[1];   // array of {path} still valid, or null == check unavailable (keep all)
        var missingByo = [];
        if (validPaths) {
          var ok = {};
          validPaths.forEach(function (v) { ok[v.path] = true; });
          byoEntries.forEach(function (e) { if (!ok[e.path]) missingByo.push(e); });
          byoEntries = byoEntries.filter(function (e) { return ok[e.path]; });
        }
        var opts = [];
        (d.bundled || []).forEach(function (w) { opts.push(wlOption(w, "bundled")); });
        (d.installed || []).forEach(function (w) { opts.push(wlOption(w, "installed")); });
        // Keep the surviving BYO entries across a refresh — the server reads them in place (not from the
        // wordlist dir), so rebuilding from bundled+installed alone would drop them + jump the selection (F05).
        byoEntries.forEach(function (e) {
          opts.push('<option value="' + esc(e.path) + '">' + esc(e.name) + " · BYO</option>");
        });
        sel.innerHTML = opts.length ? opts.join("") : '<option>no wordlists &#8212; Get more&#8230; or Bring your own&#8230;</option>';
        var stillThere = prev && Array.prototype.some.call(sel.options, function (o) { return o.value === prev; });
        if (stillThere) sel.value = prev;   // preserve the selection instead of silently jumping to the first
        var prevWasMissingByo = prev && missingByo.some(function (e) { return e.path === prev; });
        if (foot) {
          if (prevWasMissingByo) {
            foot.style.color = "var(--amber)";
            foot.textContent = "the selected wordlist is no longer on disk — pick another (it was removed)";
          } else if (prev && !stillThere) {
            foot.style.color = "var(--amber)";
            foot.textContent = "the selected wordlist is no longer available — pick another";
          } else if (d.dir) {
            foot.style.color = "";
            foot.textContent = "Wordlists in " + d.dir + " · the bundled WPA core works offline.";
          }
        }
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
      postJSON("/api/wordlists/byo", { path: p }).then(function (r) {
        // The server reads a BYO list IN PLACE (no copy), so load() — which only lists bundled+installed
        // from the wordlist dir — would never show it. Append the validated path as a selected option so
        // it's usable in the crack run right now (the run sends the option's value = the full path).
        var resolved = (r && r.path) || p;
        var name = resolved.replace(/\\/g, "/").split("/").pop() || resolved;
        // Track it so a later catalog Refresh keeps it (F05), then append + select it now.
        if (!byoEntries.some(function (e) { return e.path === resolved; })) {
          byoEntries.push({ path: resolved, name: name });
        }
        var opt = document.createElement("option");
        opt.value = resolved;
        opt.textContent = name + " · BYO";
        sel.appendChild(opt);
        sel.value = resolved;
        if (foot) { foot.style.color = "var(--green)"; foot.textContent = "✓ added + selected " + name; }
        byoPath.value = ""; byoRow.hidden = true;
      }).catch(function (err) {
        if (foot) { foot.style.color = "var(--amber)"; foot.textContent = "✗ " + (typeof err === "string" ? err : "couldn't add that file"); }
      });
    }
    if (byoAdd) byoAdd.addEventListener("click", addByo);
    if (byoPath) byoPath.addEventListener("keydown", function (e) { if (e.key === "Enter") addByo(); });
    // Desktop shell: a real OS file picker fills the path (revealed only when the native bridge exists).
    var byoBrowse = document.getElementById("wl-byo-browse");
    if (byoBrowse) byoBrowse.addEventListener("click", function () {
      nativePick("wordlist", function (path) { byoPath.value = path; addByo(); });
    });
    revealNativeControls();   // in case the bridge was already ready before this init ran
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
    // Export the captures table as CSV (display fields only — the server never writes secrets into it).
    // A programmatic <a download> keeps the SPA in place; same-origin, so the session cookie authes it.
    var exportBtn = document.getElementById("captures-export");
    if (exportBtn) exportBtn.addEventListener("click", function () {
      var a = document.createElement("a");
      a.href = "/api/captures/export";
      a.download = "cc-captures.csv";
      document.body.appendChild(a);
      a.click();
      a.remove();
    });
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
        if (m && m.cracked) {
          logLine("RECOVERED → " + (m.ssid || "") + " : " + (m.password || ""), "ok");
          // notify the network name only — never the recovered password in an OS-level notice.
          notifyDesktop("Key recovered", (m.ssid || m.key || "handshake") + " cracked");
        } else if (m) {
          logLine("[done] " + (m.detail || "key not in wordlist"), "wa");
          notifyDesktop("Crack finished", m.detail || "key not in wordlist");
        }
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
      postJSON("/api/crack/run", { consent: true, capture_key: crackSel.key, wordlist: wordlist, engine: (document.getElementById("crack-engine") || {}).value || "native", bssid: crackSel.bssid || "" })
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
        body.innerHTML = '<div class="card2">Unlock the access gate to manage nodes.</div>';
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

    function render(cams, bbox, you) {
      while (markers.firstChild) markers.removeChild(markers.firstChild);
      var s = bbox[0], w = bbox[1], n = bbox[2], e = bbox[3];
      var lonSpan = (e - w) || 1e-6, latSpan = (n - s) || 1e-6;
      var toXY = function (lat, lon) { return [((lon - w) / lonSpan) * 1000, (1 - (lat - s) / latSpan) * 380]; };
      cams.forEach(function (c) {
        var p = toXY(c.lat, c.lon);
        var halo = document.createElementNS(SVGNS, "circle");
        halo.setAttribute("cx", p[0]); halo.setAttribute("cy", p[1]); halo.setAttribute("r", 14);
        halo.setAttribute("fill", "#f85149"); halo.setAttribute("opacity", "0.12");
        var dot = document.createElementNS(SVGNS, "circle");
        dot.setAttribute("cx", p[0]); dot.setAttribute("cy", p[1]); dot.setAttribute("r", 4.5);
        dot.setAttribute("fill", "#f85149");
        if (c.label) { var t = document.createElementNS(SVGNS, "title"); t.textContent = c.label; dot.appendChild(t); }
        markers.appendChild(halo); markers.appendChild(dot);
      });
      if (you) {   // the operator's own position (centered), drawn last so it sits on top
        var yp = toXY(you.lat, you.lon);
        var yr = document.createElementNS(SVGNS, "circle");
        yr.setAttribute("cx", yp[0]); yr.setAttribute("cy", yp[1]); yr.setAttribute("r", 16);
        yr.setAttribute("fill", "var(--acc)"); yr.setAttribute("opacity", "0.18");
        var yd = document.createElementNS(SVGNS, "circle");
        yd.setAttribute("cx", yp[0]); yd.setAttribute("cy", yp[1]); yd.setAttribute("r", 5);
        yd.setAttribute("fill", "var(--acc)"); yd.setAttribute("stroke", "#fff"); yd.setAttribute("stroke-width", "1.5");
        var yt = document.createElementNS(SVGNS, "title"); yt.textContent = "you"; yd.appendChild(yt);
        markers.appendChild(yr); markers.appendChild(yd);
      }
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

    // ── GPS follow ──────────────────────────────────────────────────
    // Center on a connected device's live GPS and load only a small window around the operator.
    // Optimized: fetch a ~4km box only after moving ~800m (hysteresis), keep a tiny cache of recent
    // windows, and EVICT (sleep) any that fall far behind as you move — so memory stays bounded.
    var followBtn = document.getElementById("flock-follow");
    var gpsLabel = document.getElementById("flock-gps");
    var FETCH_HALF = 0.02, VIEW_HALF = 0.009, REFETCH = 0.008, EVICT = 0.05, MAX_CACHE = 8;
    var following = false, pollTimer = null, cache = new Map(), lastFetch = null, curPos = null, fetching = false;
    var keyOf = function (lat, lon) { return lat.toFixed(3) + "," + lon.toFixed(3); };

    function evict(lat, lon) {   // sleep windows the operator has moved away from + cap the cache
      cache.forEach(function (v, k) {
        if (Math.abs(v.lat - lat) > EVICT || Math.abs(v.lon - lon) > EVICT) cache.delete(k);
      });
      while (cache.size > MAX_CACHE) {
        var fk = null, fd = -1;
        cache.forEach(function (v, k) {
          var d = Math.abs(v.lat - lat) + Math.abs(v.lon - lon);
          if (d > fd) { fd = d; fk = k; }
        });
        if (fk) cache.delete(fk); else break;
      }
    }

    function renderFollow() {
      if (!curPos) return;
      var lat = curPos.lat, lon = curPos.lon;
      var view = [lat - VIEW_HALF, lon - VIEW_HALF, lat + VIEW_HALF, lon + VIEW_HALF];
      var seen = {}, cams = [];
      cache.forEach(function (v) {
        v.cams.forEach(function (c) {
          if (c.lat >= view[0] && c.lat <= view[2] && c.lon >= view[1] && c.lon <= view[3]) {
            var k = c.lat.toFixed(6) + "," + c.lon.toFixed(6);
            if (!seen[k]) { seen[k] = 1; cams.push(c); }
          }
        });
      });
      render(cams, view, curPos);
      if (count) count.textContent = cams.length + " ALPR nearby";
      if (attr) attr.textContent = "";
    }

    function fetchAround(lat, lon) {
      if (fetching) return;
      fetching = true;
      var bbox = [lat - FETCH_HALF, lon - FETCH_HALF, lat + FETCH_HALF, lon + FETCH_HALF].join(",");
      getJSON("/api/flock?bbox=" + encodeURIComponent(bbox)).then(function (d) {
        cache.set(keyOf(lat, lon), { lat: lat, lon: lon, cams: d.cameras || [] });
        lastFetch = { lat: lat, lon: lon };
        evict(lat, lon);
        renderFollow();
      }).catch(function () { /* offline/rate-limited: keep showing the cache */ })
        .then(function () { fetching = false; });
    }

    function gpsTick() {
      getJSON("/api/gps").then(function (g) {
        if (!g.has_fix) {
          gpsLabel.textContent = g.stale ? "GPS: fix lost" : "GPS: waiting for a fix…";
          return;
        }
        curPos = { lat: g.lat, lon: g.lon };
        gpsLabel.textContent = "GPS: " + g.lat.toFixed(4) + ", " + g.lon.toFixed(4) + " · " + (g.sats || 0) + " sat";
        var moved = !lastFetch || Math.abs(g.lat - lastFetch.lat) > REFETCH || Math.abs(g.lon - lastFetch.lon) > REFETCH;
        if (moved) fetchAround(g.lat, g.lon); else renderFollow();
      }).catch(function () { gpsLabel.textContent = "GPS: unavailable"; });
    }

    if (followBtn) followBtn.addEventListener("click", function () {
      following = !following;
      followBtn.setAttribute("aria-pressed", String(following));
      followBtn.classList.toggle("acc", following);
      if (following) {
        if (input) input.disabled = true;
        if (btn) btn.disabled = true;
        msg.textContent = "";
        gpsTick();
        pollTimer = setInterval(gpsTick, 4000);
      } else {
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = null; cache.clear(); lastFetch = null; curPos = null;
        if (input) input.disabled = false;
        if (btn) btn.disabled = false;
        gpsLabel.textContent = "GPS: no device";
      }
    });
  }
  initFlock();

  // MAP Wardrive: honest gating (no dead buttons). A survey needs a connected GPS-equipped device
  // running wardrive firmware; there's no web wardrive backend yet, so the buttons say so plainly
  // rather than doing nothing.
  function initWardrive() {
    var msg = document.getElementById("wd-msg");
    var multiMsg = document.getElementById("wd-multi-msg");
    var need = "Wardrive needs a connected GPS-equipped device running wardrive firmware. Connect one " +
      "under DEVICE, then survey + export to WiGLE CSV. (Web wardrive wiring lands with that device.)";
    wireBtn("wd-start", function () { if (msg) msg.textContent = need; });
    wireBtn("wd-export", function () { if (msg) msg.textContent = "No survey to export yet — start a GPS survey first."; });
    wireBtn("wd-multi-start", function () { if (multiMsg) multiMsg.textContent = "Needs 2+ connected GPS devices."; });

    // Upload a WiGLE CSV you already have. The survey/export above still need a GPS device, but sending an
    // already-exported CSV doesn't — so this path is live. The token comes from Settings (server-side); the
    // server rejects with a plain message if it isn't set. Browse uses the native picker on the desktop shell.
    var upRow = document.getElementById("wd-upload-row");
    var upPath = document.getElementById("wd-upload-path");
    wireBtn("wd-upload", function () { if (upRow) upRow.hidden = !upRow.hidden; });
    function doUpload() {
      var p = (upPath && upPath.value || "").trim();
      if (!p) { if (msg) { msg.style.color = "var(--amber)"; msg.textContent = "Pick a WiGLE CSV first."; } return; }
      var donate = document.getElementById("wd-upload-donate");
      var go = document.getElementById("wd-upload-go");
      if (go) { go.disabled = true; go.textContent = "Uploading…"; }
      if (msg) { msg.style.color = "var(--dim)"; msg.textContent = "Uploading to WiGLE…"; }
      postJSON("/api/wardrive/upload", { path: p, donate: !!(donate && donate.checked) }).then(function (r) {
        if (msg) {
          msg.style.color = "var(--green)";
          var tid = r && r.transid ? " (transid " + esc(r.transid) + ")" : "";
          msg.textContent = "✓ " + esc((r && r.message) || "uploaded") + tid;
        }
        notifyDesktop("WiGLE upload", (r && r.message) || "uploaded");
        if (upRow) upRow.hidden = true;
      }).catch(function (err) {
        if (msg) { msg.style.color = "var(--amber)"; msg.textContent = "✗ " + (typeof err === "string" ? err : "upload failed"); }
      }).then(function () { if (go) { go.disabled = false; go.textContent = "Upload to WiGLE"; } });
    }
    wireBtn("wd-upload-go", doUpload);
    if (upPath) upPath.addEventListener("keydown", function (e) { if (e.key === "Enter") doUpload(); });
    // Desktop shell only: a real OS file dialog fills the path (revealed when the native bridge is present).
    wireBtn("wd-upload-browse", function () { nativePick("wardrive", function (path) { if (upPath) upPath.value = path; }); });
    revealNativeControls();
  }
  initWardrive();

  // HUNT ▸ Sense — live Wi-Fi CSI presence/motion from a connected sensing node (/api/sensing).
  // Passive awareness: presence + motion only (the PROVEN tier), never identity, never a target.
  function initSense() {
    var rows = document.getElementById("sense-rows");
    if (!rows) return;
    var roomEl = document.getElementById("sense-room");
    var occEl = document.getElementById("sense-occupied");
    var sumEl = document.getElementById("sense-summary");
    var empty = '<tr><td class="off" colspan="5">no sensing node connected &#8212; flash the CSI ' +
      'node firmware + connect it to see live presence/motion</td></tr>';
    function render(d) {
      var s = (d && d.summary) || {};
      var nodes = (d && d.nodes) || [];
      if (occEl) {
        var occ = !!s.any_occupied;
        occEl.textContent = occ ? "● OCCUPIED" : "○ clear";
        occEl.className = "chip" + (occ ? " on" : "");
      }
      if (roomEl) roomEl.textContent = nodes.length ? (s.occupied + "/" + s.total + " occupied") : "—";
      if (sumEl) sumEl.textContent = nodes.length ? (s.fresh + " fresh · " + s.total + " node(s)") : "—";
      if (!nodes.length) { rows.innerHTML = empty; return; }
      rows.innerHTML = nodes.map(function (n) {
        var pres = n.presence ? '<span class="con">● present</span>' : '<span class="off">○ empty</span>';
        var stale = n.fresh ? "" : ' <span class="dim">(stale)</span>';
        var mot = (typeof n.motion === "number") ? n.motion.toFixed(2) : "—";
        var conf = (typeof n.confidence === "number") ? n.confidence.toFixed(2) : "—";
        return "<tr><td class='mono'>" + esc(n.node_id) + "</td><td class='r'>" + pres + stale +
          "</td><td class='r mono'>" + mot + "</td><td class='r mono'>" + conf +
          "</td><td class='r dim'>" + esc(n.tier || "") + "</td></tr>";
      }).join("");
    }
    function refresh() {
      getJSON("/api/sensing").then(render).catch(function () {
        rows.innerHTML = '<tr><td class="er" colspan="5">sensing unavailable</td></tr>';
      });
    }
    window.__ccRefreshSense = refresh;
    window.__ccRenderSense = render;   // exposed so a render-verify can inject seeded data
    refresh();
  }
  initSense();

  // Surface-gated, visibility-aware live cadence: one tick that refreshes ONLY the visible
  // surface's data, and stops entirely while the window is hidden/minimized — so an idle or
  // backgrounded app costs nothing on hardware. (The mockup's "live 5s", only where it's on screen.)
  refreshHealth(); refreshDevices(); refreshTargets();   // initial hydrate (once, seeds every surface)
  function _activeView() {
    var el = document.querySelector(".main .view.on");
    return el ? el.dataset.view : "";
  }
  function _activeHuntSub() {
    var el = document.querySelector('.view[data-view="hunt"] .sub.on');
    return el ? el.dataset.sub : "";
  }
  function _pollTick() {
    var v = _activeView();
    // Reconcile the shared device inventory on EVERY tick, regardless of the active view, so a device that
    // disconnects while OPERATE/Terminal is showing stops being offered/selected there (F04) — previously
    // this only ran in the DEVICE view.
    refreshDevices();
    if (v === "device") { refreshHealth(); refreshTargets(); }
    else if (v === "hunt") {
      refreshTargets();
      // only poll /api/sensing while the Sense sub-tab is actually showing (not every HUNT sub-tab)
      if (_activeHuntSub() === "sense" && window.__ccRefreshSense) window.__ccRefreshSense();
    }
    // other surfaces are socket-driven or static — no periodic poll needed
  }
  // Adaptive cadence: poll fast (5s) while the operator is active, then back off toward 20s the longer
  // the app just sits there, so an idle window costs a fraction of the requests on a small device. Any
  // nav switch, action, or re-show snaps it straight back to fast (see _forcePoll). setTimeout (not
  // setInterval) so the delay can vary tick to tick.
  var _POLL_MIN = 5000, _POLL_MAX = 20000, _pollEvery = _POLL_MIN, _pollTimer = null;
  function _schedule() {
    _pollTimer = setTimeout(function () {
      _pollTick();
      _pollEvery = Math.min(Math.round(_pollEvery * 1.5), _POLL_MAX);   // idle → slower
      _schedule();
    }, _pollEvery);
  }
  function _startPoll() { if (!_pollTimer) { _pollEvery = _POLL_MIN; _schedule(); } }
  function _stopPoll() { if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; } }
  function _forcePoll() {                    // instant refresh + reset to the fast cadence
    _pollEvery = _POLL_MIN;
    _pollTick();
    if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; _schedule(); }
  }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { _stopPoll(); }
    else { _forcePoll(); _startPoll(); }   // catch-up on re-show, then resume fast
  });
  window.__ccPollTick = _forcePoll;        // nav handlers + actions force an instant refresh on switch
  if (!document.hidden) { _startPoll(); }
})();
