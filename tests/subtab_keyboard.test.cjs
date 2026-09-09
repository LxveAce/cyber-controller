/* Subtab keyboard behavior — drives the REAL reform.js .subtabs setup/activation block in a vm context with a
 * stub DOM + inert notification spies, mirroring operate_request_order.test.cjs. No browser/app/network. Proves
 * the subtab tablists gain a single roving Tab stop and Arrow/Home/End selection+focus within their own bar
 * (skipping tabs hidden by Simple mode, decided at event time), while preserving click activation, the panel/
 * crumb/notification behavior (exactly once per activation), native Enter/Space (not double-activated), and
 * two-bar isolation.
 */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm"), path = require("node:path");

const source = fs.readFileSync(path.join(__dirname, "../src/ui/web/static/reform.js"), "utf8");
const start = source.indexOf('document.querySelectorAll(".subtabs").forEach(function (bar) {');
const end = source.indexOf("\n  updateFwMode();   // reflect whatever sub-tab the page loaded with");
assert(start >= 0 && end > start, "subtabs block markers not found");
const block = source.slice(start, end);

function classList(initial) {
  const s = new Set(initial || []);
  return {
    contains: (c) => s.has(c),
    toggle: (c, v) => { v = (v === undefined) ? !s.has(c) : v; if (v) s.add(c); else s.delete(c); return v; },
    add: (c) => s.add(c), remove: (c) => s.delete(c),
  };
}

function setup(src) {
  const state = { active: null, mesh: 0, incidents: 0, fw: 0, poll: 0 };

  function mkBtn(spec) {
    const cls = ["tab-btn"]; if (spec.on) cls.push("on"); if (spec.pro) cls.push("pro-tab");
    const b = {
      dataset: { sub: spec.sub }, textContent: spec.sub, classList: classList(cls), _a: {}, focused: 0,
      setAttribute(k, v) { this._a[k] = String(v); },
      getAttribute(k) { return (k in this._a) ? this._a[k] : null; },
      closest(sel) { return sel === "button" ? b : null; },
      focus() { b.focused++; state.active = b; },
    };
    return b;
  }
  function mkPanel(spec) { return { dataset: { sub: spec.sub }, classList: classList(spec.on ? ["on"] : []) }; }
  function mkBar(tabs, view, specs) {
    const btns = specs.map(mkBtn);
    const panels = specs.map(mkPanel);
    const scope = { dataset: { view }, querySelectorAll: (sel) => (sel === ":scope > .sub" ? panels : []) };
    const bar = {
      dataset: { tabs }, parentElement: scope, _h: {},
      setAttribute() {},
      querySelectorAll: (sel) => (sel === "button" ? btns : []),
      addEventListener(t, fn) { (this._h[t] = this._h[t] || []).push(fn); },
      fire(t, ev) { (this._h[t] || []).forEach((fn) => fn(ev)); },
      _btns: btns, _panels: panels,
    };
    return bar;
  }

  const bar1 = mkBar("device", "device", [{ sub: "dash", on: true }, { sub: "logs" }, { sub: "mesh" }]);
  const bar2 = mkBar("xcomm", "device", [{ sub: "pool", on: true }, { sub: "live" }]);   // nested, separate scope
  const bar3 = mkBar("hunt", "hunt",
    [{ sub: "one", on: true }, { sub: "p1", pro: true }, { sub: "p2", pro: true }, { sub: "two" }]);
  const bar4 = mkBar("operate", "operate", [{ sub: "solo", on: true }, { sub: "pp", pro: true }]);
  const bars = [bar1, bar2, bar3, bar4];

  const app = { classList: classList([]) };   // Simple mode = app.classList has "pro-hidden"
  const document = {
    querySelectorAll: (sel) => (sel === ".subtabs" ? bars : []),
    getElementById: (id) => (id === "app" ? app : null),
    activeElement: null,
  };
  const window = {
    CCMeshStatus: { syncVisibility() { state.mesh++; } },
    CCIncidents: { syncVisibility() { state.incidents++; } },
    __ccPollTick() { state.poll++; },
  };
  const crumb = { innerHTML: "" };
  const crumbNames = { device: "DEVICE", hunt: "HUNT", operate: "OPERATE" };
  const updateFwMode = () => { state.fw++; };

  const ctx = vm.createContext({ document, window, crumb, crumbNames, updateFwMode });
  vm.runInContext(src, ctx);
  return { bar1, bar2, bar3, bar4, app, document, crumb, state };
}

const evt = (key) => { const e = { key, _pd: 0, preventDefault() { e._pd++; } }; return e; };
const tab = (b) => b._a.tabindex;
const onBtn = (bar) => bar._btns.find((b) => b.classList.contains("on"));
const onPanels = (bar) => bar._panels.filter((p) => p.classList.contains("on")).map((p) => p.dataset.sub);

// ── initial state / click / arrows / native keys / isolation (default = Pro; all tabs visible) ──────

test("setup gives each bar a single roving Tab stop with roles + selected state", () => {
  const f = setup(block);
  assert.deepEqual(f.bar1._btns.map(tab), ["0", "-1", "-1"]);
  assert.deepEqual(f.bar1._btns.map((b) => b._a["aria-selected"]), ["true", "false", "false"]);
  assert.deepEqual(f.bar1._btns.map((b) => b._a.role), ["tab", "tab", "tab"]);
});

test("click activates the button, switches its panel, and fires each notification exactly once", () => {
  const f = setup(block);
  f.bar1.fire("click", { target: f.bar1._btns[1] });
  assert.equal(onBtn(f.bar1).dataset.sub, "logs");
  assert.deepEqual(f.bar1._btns.map(tab), ["-1", "0", "-1"]);
  assert.deepEqual(onPanels(f.bar1), ["logs"]);
  assert.equal(f.state.mesh, 1); assert.equal(f.state.incidents, 1);
  assert.equal(f.state.fw, 1); assert.equal(f.state.poll, 1);
});

test("ArrowRight moves selection + focus forward and wraps; ArrowLeft wraps back; Home/End", () => {
  const f = setup(block);
  f.document.activeElement = f.bar1._btns[0];
  const e = evt("ArrowRight");
  f.bar1.fire("keydown", e);
  assert.equal(e._pd, 1);
  assert.equal(onBtn(f.bar1).dataset.sub, "logs");
  assert.equal(f.bar1._btns[1].focused, 1);
  assert.equal(f.state.mesh, 1);
  f.document.activeElement = f.bar1._btns[2];
  f.bar1.fire("keydown", evt("ArrowRight"));
  assert.equal(onBtn(f.bar1).dataset.sub, "dash");                  // wrap
  f.document.activeElement = f.bar1._btns[0];
  f.bar1.fire("keydown", evt("ArrowLeft"));
  assert.equal(onBtn(f.bar1).dataset.sub, "mesh");                  // back-wrap
  f.document.activeElement = f.bar1._btns[1];
  f.bar1.fire("keydown", evt("Home"));
  assert.equal(onBtn(f.bar1).dataset.sub, "dash");
  f.document.activeElement = f.bar1._btns[0];
  f.bar1.fire("keydown", evt("End"));
  assert.equal(onBtn(f.bar1).dataset.sub, "mesh");
});

test("Enter/Space are not handled by the keydown (native click activates; no duplicate)", () => {
  const f = setup(block);
  f.document.activeElement = f.bar1._btns[0];
  const before = f.state.mesh;
  const e = evt("Enter");
  f.bar1.fire("keydown", e);
  assert.equal(e._pd, 0);
  assert.equal(onBtn(f.bar1).dataset.sub, "dash");
  assert.equal(f.state.mesh, before);
});

test("a keydown while focus is in another bar does not move this bar", () => {
  const f = setup(block);
  f.document.activeElement = f.bar2._btns[0];
  const before = f.state.mesh;
  f.bar1.fire("keydown", evt("ArrowRight"));
  assert.equal(onBtn(f.bar1).dataset.sub, "dash");
  assert.equal(f.state.mesh, before);
});

// ── Simple mode: Arrow/Home/End SKIP tabs hidden by Simple mode, decided at event time ──

test("Pro mode (default): pro-tabs are navigable", () => {
  const f = setup(block);
  f.document.activeElement = f.bar3._btns[0];                       // "one"
  f.bar3.fire("keydown", evt("ArrowRight"));
  assert.equal(onBtn(f.bar3).dataset.sub, "p1");                   // reaches the pro tab in Pro mode
});

test("Simple mode: ArrowRight skips the hidden pro-tabs to the next visible tab", () => {
  const f = setup(block);
  f.app.classList.add("pro-hidden");                               // enter Simple mode
  f.document.activeElement = f.bar3._btns[0];                       // "one"
  const e = evt("ArrowRight");
  f.bar3.fire("keydown", e);
  assert.equal(e._pd, 1);
  assert.equal(onBtn(f.bar3).dataset.sub, "two", "skips p1/p2 (hidden) to two");
  assert.equal(f.bar3._btns[3].focused, 1);
  assert.equal(tab(f.bar3._btns[3]), "0");
});

test("Simple mode: ArrowLeft wraps backward over hidden pro-tabs; Home/End land on visible ends", () => {
  const f = setup(block);
  f.app.classList.add("pro-hidden");
  f.document.activeElement = f.bar3._btns[0];                       // "one"
  f.bar3.fire("keydown", evt("ArrowLeft"));
  assert.equal(onBtn(f.bar3).dataset.sub, "two", "back-wrap skips p2/p1 to two");
  f.document.activeElement = f.bar3._btns[3];                       // "two"
  f.bar3.fire("keydown", evt("Home"));
  assert.equal(onBtn(f.bar3).dataset.sub, "one", "Home = first visible");
  f.document.activeElement = f.bar3._btns[0];
  f.bar3.fire("keydown", evt("End"));
  assert.equal(onBtn(f.bar3).dataset.sub, "two", "End = last visible");
});

test("Simple mode: a bar with one visible tab does not move or re-fire on Arrow", () => {
  const f = setup(block);
  f.app.classList.add("pro-hidden");
  f.document.activeElement = f.bar4._btns[0];                       // "solo" (pp is a hidden pro-tab)
  const before = f.state.mesh;
  const e = evt("ArrowRight");
  f.bar4.fire("keydown", e);
  assert.equal(e._pd, 1, "the key is consumed");
  assert.equal(onBtn(f.bar4).dataset.sub, "solo", "no move — pp is hidden");
  assert.equal(f.state.mesh, before, "no re-activation notification");
});

test("mode change after setup is honored: Pro reaches p1, then Simple skips it", () => {
  const f = setup(block);
  f.document.activeElement = f.bar3._btns[0];
  f.bar3.fire("keydown", evt("ArrowRight"));
  assert.equal(onBtn(f.bar3).dataset.sub, "p1");                   // Pro
  f.app.classList.add("pro-hidden");                               // switch to Simple AFTER setup
  f.document.activeElement = f.bar3._btns[0];
  f.bar3.fire("keydown", evt("ArrowRight"));
  assert.equal(onBtn(f.bar3).dataset.sub, "two", "now skips the hidden pro-tabs");
});
