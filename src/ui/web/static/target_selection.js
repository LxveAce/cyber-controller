/* Local target selection and device drafts. This module has no network or device writer. */
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) module.exports = factory(require("./target_snapshots.js"));
  else root.CCTargetSelection = factory(root.CCTargetSnapshots);
})(typeof globalThis !== "undefined" ? globalThis : this, function (snapshots) {
  "use strict";
  const TYPES = new Set(["ap", "client", "ble", "subghz", "nfc", "rfid", "alpr"]);
  const CAP = 128;
  const clone = value => JSON.parse(JSON.stringify(value));
  const text = (value, max) => typeof value === "string" && value.length <= max && !value.includes("\0");
  function key(row) {
    return row && TYPES.has(row.target_type) && text(row.mac, 256) && row.mac ? row.target_type + ":" + row.mac : null;
  }
  function timestamp(value) {
    return snapshots.validTimestamp(value);
  }
  function create(options) {
    options = options || {};
    let records = new Map(), selected = new Map(), devices = new Map(), drafts = new Map();
    let anchor = null, revision = 0, message = "", authorized = true;
    let selectionEpoch = 0, priorSelection = JSON.stringify([true, []]);
    function view() {
      return clone({ selected: Array.from(selected.values()), drafts: Array.from(drafts.values()),
        devices: Array.from(devices.values()), revision, message, authorized, cap: CAP });
    }
    function selectionSignature() {
      return JSON.stringify([authorized, Array.from(selected.values()).map(e =>
        [e.key, e.record, e.stale, e.reason])]);
    }
    function menuSignature() {
      return JSON.stringify([selectionSignature(), Array.from(devices.values()).sort((a,b)=>a.port<b.port?-1:a.port>b.port?1:0),
        Array.from(drafts.values()).map(d => [d.port, d.available, d.reason,
          d.targets.map(e => [e.key, e.record.timestamp, e.stale, e.reason])])]);
    }
    function publish(note) {
      const identity = selectionSignature();
      if (identity !== priorSelection) { priorSelection = identity; selectionEpoch++; }
      if (note !== undefined) message = note;
      if (options.onChange) { try { options.onChange(view()); } catch (_) { /* Rendering does not own the draft state. */ } }
    }
    function eligible(k) { return authorized && records.has(k) && timestamp(records.get(k).timestamp); }
    function item(k) { return { key: k, record: clone(records.get(k)), stale: false, reason: "" }; }
    function stale(entry, reason) { entry.stale = true; entry.reason = reason; }
    function snapshot(rows) {
      if (!Array.isArray(rows) || rows.length > snapshots.MAX_ROWS) throw new Error("Invalid local target snapshot");
      const next = new Map(), duplicates = new Set();
      rows.forEach(function (row) {
        const k = key(row);
        if (!k) return;
        if (next.has(k)) duplicates.add(k);
        next.set(k, { target_type: row.target_type, mac: row.mac, ssid: text(row.ssid, 8192) ? row.ssid : "",
          timestamp: timestamp(row.timestamp) ? row.timestamp : "", device_source: text(row.device_source, 256) ? row.device_source : "" });
      });
      duplicates.forEach(k => next.delete(k));
      records = next; authorized = true; revision++;
      function reconcile(entry) {
        const live = next.get(entry.key);
        if (!live) stale(entry, "Record removed; select it again if it returns.");
        else if (!live.timestamp || live.timestamp !== entry.record.timestamp) stale(entry, "Record replaced; select it again.");
        // A stale entry is intentionally never revived by a later snapshot.
        else if (!entry.stale) entry.record = clone(live);
      }
      selected.forEach(reconcile);
      drafts.forEach(draft => draft.targets.forEach(reconcile));
      publish();
    }
    function setDevices(rows) {
      const next = new Map();
      rows.forEach(function (row) {
        if (row && text(row.port, 256) && row.port && row.connected === true) {
          next.set(row.port, { port: row.port, label: text(row.name, 512) ? row.name : "" });
        }
      });
      const lost = new Set(Array.from(devices.keys()).filter(port => !next.has(port)));
      function mark(entry) {
        if (lost.has(entry.record.device_source)) stale(entry, "Source disconnected; select the record again.");
      }
      selected.forEach(mark);
      drafts.forEach(function (draft) {
        if (!next.has(draft.port)) { draft.available = false; draft.reason = "Device disconnected; create a new draft to use it again."; }
        draft.targets.forEach(mark);
      });
      devices = next; publish();
    }
    function select(k, mode, order) {
      if (!eligible(k)) { publish("This record is unavailable for local selection."); return false; }
      let keys;
      if (mode === "range" && anchor && order.includes(anchor) && order.includes(k)) {
        const a = order.indexOf(anchor), b = order.indexOf(k);
        keys = order.slice(Math.min(a,b), Math.max(a,b)+1);
      } else keys = [k];
      if (keys.some(k => !eligible(k))) { publish("The range contains unavailable records; selection was unchanged."); return false; }
      const next = mode === "replace" ? new Map() : new Map(selected);
      if (mode === "toggle" && next.has(k) && !next.get(k).stale) next.delete(k);
      else keys.forEach(k => next.set(k, item(k)));
      if (next.size > CAP) { publish("Select up to 128 records. Selection was unchanged."); return false; }
      selected = next; if (mode !== "range") anchor = k;
      publish(""); return true;
    }
    function selectVisible(order) {
      const next = new Map(selected);
      if (order.some(k => !eligible(k))) { publish("Some visible records are unavailable; selection was unchanged."); return false; }
      order.forEach(k => next.set(k, item(k)));
      if (next.size > CAP) { publish("Select up to 128 records. Selection was unchanged."); return false; }
      selected = next; anchor = order[0] || null; publish(""); return true;
    }
    function clear() { selected.clear(); anchor = null; publish("Local selection cleared. Device drafts are unchanged."); }
    function remove(k) { selected.delete(k); if (anchor === k) anchor = null; publish(""); }
    function fresh() { return selected.size > 0 && Array.from(selected.values()).every(e => !e.stale && eligible(e.key)); }
    function copyValue(field) {
      if (!fresh()) return null;
      const entries = Array.from(selected.values());
      const valid = field === "mac" ? e => ["ap","client","ble","alpr"].includes(e.record.target_type) && /^(?:[\da-fA-F]{2}:){5}[\da-fA-F]{2}$/.test(e.record.mac) :
        field === "ssid" ? e => ["ap","client"].includes(e.record.target_type) && !!e.record.ssid : () => false;
      return entries.every(valid) ? entries.map(e => e.record[field]).join("\n") : null;
    }
    function assign(ports) {
      if (!fresh() || !Array.isArray(ports) || !ports.length || ports.length > CAP || new Set(ports).size !== ports.length || ports.some(p => !devices.has(p))) {
        publish("Choose current records and connected devices before creating local drafts."); return false;
      }
      // Assignment replaces the local draft set and freezes exactly this device membership.
      drafts = new Map(ports.map(port => [port, { port, available: true, reason: "", targets: clone(Array.from(selected.values())) }]));
      publish("Local drafts saved. No targets were sent to a device."); return true;
    }
    function authLoss() {
      records.clear(); selected.clear(); drafts.clear(); devices.clear(); anchor = null; authorized = false; revision++;
      publish("Sign in again. Local selection and device drafts were cleared.");
    }
    return { view, snapshot, setDevices, select, selectVisible, clear, remove, assign, authLoss, copyValue, selectionSignature, menuSignature,
      eligible, fresh, notice: publish, selectionRevision: () => selectionEpoch,
      getDraft: port => drafts.has(port) ? clone(drafts.get(port)) : null,
      clearDrafts: function () { drafts.clear(); publish("Local device drafts cleared."); } };
  }

  function mount(options) {
    const doc = options.document || document, host = options.host, menu = doc.getElementById("hunt-target-menu");
    const picker = doc.getElementById("hunt-draft-picker"), summary = doc.getElementById("hunt-selection-summary");
    let opener = null, openerRect = null, focusMemo = null, pickerSelection = null, copying = 0, selectedSignature = null;
    let menuIdentity = null, menuRow = null, menuEpoch = 0;
    const bodies = ["hunt-wifi-rows", "hunt-ble-rows", "hunt-targets-rows"].map(id => doc.getElementById(id));
    function actionButton(body) { return doc.querySelector('[data-hunt-actions="'+body.id+'"]'); }
    function rows(body) { return Array.from(body.querySelectorAll("tr[data-target-key]")); }
    function order(body) { return rows(body).map(row => row.dataset.targetKey); }
    function rowFor(control) { return control && control.closest ? control.closest("tr[data-target-key]") : null; }
    function checkGeometry() {
      if (menu.hidden || !opener || !openerRect) return;
      const rect=opener.getBoundingClientRect();
      if(!opener.isConnected || rect.left!==openerRect.left || rect.top!==openerRect.top || rect.width!==openerRect.width || rect.height!==openerRect.height)close(true);
    }
    function restoreFocus() {
      if (!opener) return;
      const target = opener.isConnected ? opener : focusMemo && actionButton(focusMemo.body);
      if (target) target.focus();
    }
    function close(restore) {
      menuEpoch++;
      menu.hidden = true; picker.hidden = true; pickerSelection = null;
      menuIdentity = null; menuRow = null;
      doc.querySelectorAll("[data-hunt-actions]").forEach(b => b.setAttribute("aria-expanded", "false"));
      if (opener && opener.isConnected && opener.hasAttribute("aria-expanded")) opener.setAttribute("aria-expanded","false");
      if (restore) restoreFocus();
    }
    function refresh(state) {
      if (!menu.hidden && menuIdentity !== control.menuSignature()) close(true);
      const selected = new Map(state.selected.map(e => [e.key,e]));
      bodies.forEach(function (body) {
        rows(body).forEach(function (row) {
          const entry = selected.get(row.dataset.targetKey), box = row.querySelector("input[data-target-check]");
          row.classList.toggle("target-selected", !!entry); row.classList.toggle("target-stale", !!(entry && entry.stale));
          if (box) { box.checked = !!entry && !entry.stale; box.disabled = !control.eligible(row.dataset.targetKey); box.title=box.disabled?"Record identity or first-seen time is unavailable.":entry&&entry.stale?"Select again to replace this unavailable local selection.":""; }
        });
      });
      const count = state.selected.length, stale = state.selected.filter(e => e.stale).length;
      doc.querySelectorAll("[data-hunt-count]").forEach(el => { el.textContent = count + " selected" + (stale ? " · "+stale+" unavailable" : ""); });
      const status = doc.getElementById("hunt-selection-status");
      if (status.textContent !== state.message) status.textContent = state.message;
      summary.hidden = count === 0 && state.drafts.length === 0;
      const list = doc.getElementById("hunt-selected-list");
      const signature=JSON.stringify(state.selected.map(e=>[e.key,e.record.ssid,e.record.target_type,e.stale,e.reason]));
      if(signature!==selectedSignature){
      const focused=list.contains(doc.activeElement)?doc.activeElement.getAttribute("data-remove-target"):null;
      list.textContent=""; selectedSignature=signature;
      state.selected.forEach(function (entry) {
        const li = doc.createElement("li"), remove = doc.createElement("button");
        const label = entry.record.ssid || entry.record.mac;
        li.textContent = label + " (" + entry.record.target_type + ")" + (entry.stale ? " — unavailable: " + entry.reason : "");
        remove.type = "button"; remove.className = "btn sm"; remove.textContent = "Remove";
        remove.setAttribute("data-remove-target",entry.key);
        remove.setAttribute("aria-label", "Remove " + label + " from local selection");
        remove.addEventListener("click", function () { control.remove(entry.key); }); li.appendChild(remove); list.appendChild(li);
      });
      if(focused){const next=Array.from(list.querySelectorAll("button")).find(b=>b.getAttribute("data-remove-target")===focused) || host.querySelector('.sub.on [data-hunt-actions]');if(next)next.focus();}
      }
      doc.getElementById("hunt-drafts-summary").textContent = state.drafts.length ? "Local device drafts: " + state.drafts.map(d => d.port + (!d.available || d.targets.some(e=>e.stale) ? " (unavailable)" : "")).join(", ") : "No local device drafts.";
      if (options.onDraftChange) options.onDraftChange(state);
    }
    const control = create({onChange: refresh});
    function selectedIdentity() { return JSON.stringify(control.view().selected.map(e=>[e.key,e.record.timestamp,e.stale])); }
    function button(label, enabled, action) {
      const b = doc.createElement("button"); b.type="button"; b.setAttribute("role","menuitem"); b.tabIndex=-1;
      const epoch = menuEpoch;
      b.textContent=label; b.disabled=!enabled; b.addEventListener("click",function () {
        if (epoch !== menuEpoch || b.parentElement !== menu || menu.hidden || b.disabled) return;
        if (menuIdentity !== control.menuSignature()) { close(true); return; }
        action();
      }); menu.appendChild(b); return b;
    }
    function openPicker() {
      close(false); picker.hidden=false; pickerSelection=selectedIdentity();
      doc.getElementById("hunt-draft-status").textContent="";
      const fields=doc.getElementById("hunt-draft-devices"); fields.textContent="";
      const legend=doc.createElement("legend");legend.textContent="Connected devices";fields.appendChild(legend);
      control.view().devices.forEach(function (d) {
        const label=doc.createElement("label"), box=doc.createElement("input"); box.type="checkbox"; box.value=d.port;
        label.appendChild(box); label.appendChild(doc.createTextNode(" "+d.port+(d.label ? " — "+d.label : ""))); fields.appendChild(label);
      });
      const first=fields.querySelector("input") || doc.getElementById("hunt-draft-cancel"); first.focus();
    }
    function openMenu(source, row, x, y) {
      close(false); opener=source;
      if (row && !control.view().selected.some(e=>e.key===row.dataset.targetKey && !e.stale) &&
          !control.select(row.dataset.targetKey,"replace",order(row.parentElement))) return;
      const state=control.view(); menu.textContent="";
      const selectionAtOpen=control.selectionSignature();
      const selectionEpochAtOpen=control.selectionRevision();
      function sameSelection() { return control.selectionRevision()===selectionEpochAtOpen && control.selectionSignature()===selectionAtOpen; }
      function copy(field) {
        const value=control.copyValue(field), token=++copying;
        close(true); if (value === null) return;
        Promise.resolve().then(function () {
          const current=control.view();if(token!==copying || !current.authorized || !sameSelection())return false;
          return Promise.resolve((options.copy || (text=>navigator.clipboard.writeText(text)))(value)).then(()=>true);
        }).then(function (wrote) {
          if (wrote && token===copying && control.view().authorized && sameSelection()) control.notice("Copied "+(field==="mac"?"MAC address":"SSID")+(state.selected.length>1?" values.":"."));
        }, function () { if (token===copying && control.view().authorized && sameSelection()) control.notice("Clipboard access failed. Nothing was copied."); });
      }
      button(state.selected.length>1?"Copy MAC addresses":"Copy MAC", control.copyValue("mac")!==null, ()=>copy("mac"));
      button(state.selected.length>1?"Copy SSIDs":"Copy SSID", control.copyValue("ssid")!==null, ()=>copy("ssid"));
      button("Create local device drafts…",control.fresh() && state.devices.length>0,openPicker);
      state.drafts.forEach(d=>button("Open in Operate · "+d.port+(d.available?"":" (unavailable)"),true,function () { close(false); options.openOperate(control.getDraft(d.port)); }));
      button("Clear local selection",state.selected.length>0,function () { close(true);control.clear(); });
      button("Clear local device drafts",state.drafts.length>0,function () { close(true);control.clearDrafts(); });
      menuIdentity=control.menuSignature();
      const sourceRow=rowFor(source);
      menuRow=sourceRow?{key:sourceRow.dataset.targetKey,body:sourceRow.parentElement}:null;
      menu.hidden=false; source.setAttribute("aria-expanded","true");
      const rect=source.getBoundingClientRect(), width=doc.documentElement.clientWidth, height=doc.documentElement.clientHeight;
      openerRect={left:rect.left,top:rect.top,width:rect.width,height:rect.height};
      menu.style.left=Math.max(8,Math.min(x===undefined?rect.left:x,width-menu.offsetWidth-8))+"px";
      menu.style.top=Math.max(8,Math.min(y===undefined?rect.bottom:y,height-menu.offsetHeight-8))+"px";
      const first=menu.querySelector("button:not(:disabled)"); if(first) {first.tabIndex=0;first.focus();} else {menu.tabIndex=-1;menu.focus();}
    }
    bodies.forEach(function (body) {
      body.addEventListener("click",function (event) {
        const row=rowFor(event.target); if(!row) return;
        const mode=event.shiftKey?"range":event.target.matches("input")||event.ctrlKey||event.metaKey?"toggle":"replace";
        control.select(row.dataset.targetKey,mode,order(body));
      });
      body.addEventListener("contextmenu",function(event){const row=rowFor(event.target);if(!row)return;event.preventDefault();openMenu(row.querySelector("input"),row,event.clientX,event.clientY);});
      body.addEventListener("keydown",function(event){if(event.key==="ContextMenu"||(event.shiftKey&&event.key==="F10")){const row=rowFor(event.target);if(row){event.preventDefault();openMenu(event.target,row);}}});
      actionButton(body).addEventListener("click",event=>openMenu(event.currentTarget,null));
      actionButton(body).addEventListener("keydown",function(event){if(event.key==="ContextMenu"||(event.shiftKey&&event.key==="F10")){event.preventDefault();openMenu(event.currentTarget,null);}});
      doc.querySelector('[data-hunt-visible="'+body.id+'"]').addEventListener("click",()=>control.selectVisible(order(body)));
    });
    menu.addEventListener("keydown",function(event){
      if(event.key==="Escape"){event.preventDefault();close(true);return;}
      if(event.key==="Tab"){close(false);return;}
      const items=Array.from(menu.querySelectorAll("button:not(:disabled)"));if(!items.length)return;
      let index=items.indexOf(doc.activeElement);
      if(event.key==="ArrowDown")index=(index+1)%items.length;else if(event.key==="ArrowUp")index=(index-1+items.length)%items.length;
      else if(event.key==="Home")index=0;else if(event.key==="End")index=items.length-1;else return;
      event.preventDefault();items.forEach(b=>b.tabIndex=-1);items[index].tabIndex=0;items[index].focus();
    });
    doc.addEventListener("pointerdown",event=>{if(!menu.hidden&&!menu.contains(event.target)&&event.target!==opener)close(false);});
    doc.addEventListener("keydown",event=>{if(!picker.hidden&&event.key==="Escape"){event.preventDefault();close(true);}else if(!picker.hidden&&event.key==="Tab"){
      const items=Array.from(picker.querySelectorAll("input,button")),index=items.indexOf(doc.activeElement);
      if(event.shiftKey&&index===0){event.preventDefault();items[items.length-1].focus();}else if(!event.shiftKey&&index===items.length-1){event.preventDefault();items[0].focus();}
    }});
    doc.getElementById("hunt-draft-cancel").addEventListener("click",()=>close(true));
    doc.getElementById("hunt-draft-save").addEventListener("click",function(){
      if(pickerSelection!==selectedIdentity()){doc.getElementById("hunt-draft-status").textContent="Selection changed. Cancel and review it before creating drafts.";return;}
      const ports=Array.from(picker.querySelectorAll("input:checked")).map(box=>box.value);
      if(control.assign(ports))close(true);else doc.getElementById("hunt-draft-status").textContent=control.view().message;
    });
    doc.defaultView.addEventListener("resize",()=>close(true));
    doc.getElementById("main").addEventListener("scroll",function(){
      if(menu.hidden || !opener || !openerRect)return;
      const rect=opener.getBoundingClientRect();
      // A focus/click can deliver an already-completed scroll event after opening.
      if(rect.left!==openerRect.left || rect.top!==openerRect.top || rect.width!==openerRect.width || rect.height!==openerRect.height)close(false);
    });
    host.querySelector(".subtabs").addEventListener("click",()=>close(false));
    doc.getElementById("rail").addEventListener("click",()=>close(false));
    function beforeRender(){
      const row=rowFor(doc.activeElement) || (!menu.hidden && rowFor(opener));
      focusMemo=row?{key:row.dataset.targetKey,body:row.parentElement}:null;
    }
    function afterRender(){
      bodies.forEach(body=>rows(body).forEach(function(row){
        const cell=doc.createElement("td"),box=doc.createElement("input");box.type="checkbox";box.dataset.targetCheck="";
        box.setAttribute("aria-label","Select "+row.dataset.targetLabel);box.setAttribute("aria-haspopup","menu");box.setAttribute("aria-controls",menu.id);box.setAttribute("aria-expanded","false");
        cell.className="target-checkbox";cell.appendChild(box);row.insertBefore(cell,row.firstChild);
      }));
      refresh(control.view());
      if(!menu.hidden){
        const row=menuRow && rows(menuRow.body).find(r=>r.dataset.targetKey===menuRow.key);
        const next=menuRow?(row && row.querySelector("input")):opener;
        if(!next || !next.isConnected)close(true);
        else {
          opener=next;
          checkGeometry();
          if(!menu.hidden)opener.setAttribute("aria-expanded","true");
        }
      }
      if(focusMemo && menu.hidden){const row=rows(focusMemo.body).find(r=>r.dataset.targetKey===focusMemo.key);(row?row.querySelector("input"):actionButton(focusMemo.body)).focus();}
      focusMemo=null;
    }
    refresh(control.view());
    return {control,beforeRender,afterRender,checkGeometry,authLoss:function(){copying++;close(true);control.authLoss();},close};
  }
  return {create,mount,key,timestamp,CAP};
});
