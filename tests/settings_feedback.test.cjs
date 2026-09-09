/* General Settings save/reload feedback — drives the REAL reform.js general-settings block in a vm context with
 * stub DOM + getJSON/postJSON, mirroring operate_request_order.test.cjs. No browser/app/network. Covers the
 * deferred audit: a "saved ✓" confirmation must not survive a later edit (else the displayed saved-state stops
 * matching the form), edits are blocked during an in-flight save, and the retry-button label is not left stale.
 */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm"), path = require("node:path");

const source = fs.readFileSync(path.join(__dirname, "../src/ui/web/static/reform.js"), "utf8");
// The contiguous general-settings block: the chip (switch) wiring through the end of the Reset handler.
const start = source.indexOf("// Every chip is a keyboard-operable switch");
const endAnchor = source.indexOf('setStatus("reset failed"', start);
const end = source.indexOf("\n    });", endAnchor) + "\n    });".length;
assert(start >= 0 && endAnchor > start && end > endAnchor, "settings block markers not found");
const block = source.slice(start, end);

function makeEl() {
  return {
    value: "", textContent: "", hidden: false, disabled: false, _on: false, _attrs: {}, _h: {}, style: {},
    addEventListener(t, fn) { (this._h[t] = this._h[t] || []).push(fn); },
    getAttribute(k) { return (k in this._attrs) ? this._attrs[k] : null; },
    setAttribute(k, v) { this._attrs[k] = String(v); },
    matches() { return true; },
    fire(t) { (this._h[t] || []).forEach(function (fn) { fn({ key: "", preventDefault() {} }); }); },
  };
}

function setup(src) {
  const els = {}, calls = [];
  const document = { getElementById(id) { return els[id] || (els[id] = makeEl()); } };
  const ctx = vm.createContext({
    document,
    getJSON(url) { return new Promise(function (res, rej) { calls.push({ url, type: "get", res, rej }); }); },
    postJSON(url, body) { return new Promise(function (res, rej) { calls.push({ url, body, type: "post", res, rej }); }); },
    chipOn(el) { return !!(el && el._on); },
    setChip(el, on) { if (el) el._on = !!on; },
    setSelect(el, val) { if (el) el.value = String(val); },
  });
  vm.runInContext(src, ctx);
  return {
    els, calls,
    el(id) { return els[id] || (els[id] = makeEl()); },
    status() { return els["set-status"].textContent; },
    post() { return calls.find(function (c) { return c.type === "post"; }); },
  };
}

const VALID = {
  ok: true,
  settings: {
    serial: { default_baud: 115200 }, flash: { flash_baud: null }, interface: { touch_mode: "auto" },
    updates: { enabled: true }, safety: { confirm_dangerous: true, suppress_all_warnings: false },
    security: { secure_container: false }, vault: { dir: "/data/vault" }, uploads: { wigle_token_set: false },
  },
};
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

async function loaded(src) {                 // a harness with the initial /api/settings load already resolved
  const f = setup(src);
  f.calls[0].res(VALID);
  await flush();
  return f;
}

async function savedOk(src) {                // ...and one successful save applied ("saved ✓")
  const f = await loaded(src);
  f.el("set-save").fire("click");
  await flush();
  f.post().res(VALID);
  await flush();
  return f;
}

// ── the fix: a "saved ✓" confirmation is invalidated by the next edit ──────────────────────────────

test("initial load clears the status and marks the form ready", async () => {
  const f = await loaded(block);
  assert.equal(f.status(), "");
  f.el("set-save").fire("click"); await flush();
  assert.ok(f.post(), "a ready form allows a save POST");
});

test("saved shows the confirmation", async () => {
  const f = await savedOk(block);
  assert.equal(f.status(), "saved ✓");
});

test("toggling a settings switch after a save clears the stale 'saved ✓' confirmation", async () => {
  const f = await savedOk(block);
  assert.equal(f.status(), "saved ✓");
  f.el("set-updates-enabled").fire("click");           // an edit
  assert.equal(f.status(), "", "the confirmation must not survive an edit");
});

test("editing an input after a save clears the stale 'saved ✓' confirmation", async () => {
  const f = await savedOk(block);
  f.el("set-serial-baud").fire("change");
  assert.equal(f.status(), "");
});

// ── sound behaviors it must preserve ────────────────────────────────────────────────────────────────

test("a switch cannot be toggled during an in-flight save (controls locked), and success still shows", async () => {
  const f = await loaded(block);
  f.el("set-save").fire("click");                      // writing -> controls locked
  const before = f.el("set-updates-enabled")._on;
  assert.equal(f.el("set-updates-enabled").getAttribute("aria-disabled"), "true");
  f.el("set-updates-enabled").fire("click");           // ignored while writing
  assert.equal(f.el("set-updates-enabled")._on, before);
  f.post().res(VALID); await flush();
  assert.equal(f.status(), "saved ✓");
});

// ── the retry-button label is not left stale after an unconfirmable save ─────────────────────────────

test("retry label resets to 'Retry loading' when reloading after an unconfirmable save", async () => {
  const f = await loaded(block);
  f.el("set-save").fire("click"); await flush();
  f.post().res({ ok: false });                         // unconfirmable -> "Reload saved settings"
  await flush();
  assert.equal(f.el("set-retry").textContent, "Reload saved settings");
  assert.equal(f.el("set-retry").hidden, false);
  f.el("set-retry").fire("click"); await flush();      // reload -> label reset at load start
  assert.equal(f.el("set-retry").textContent, "Retry loading");
});
