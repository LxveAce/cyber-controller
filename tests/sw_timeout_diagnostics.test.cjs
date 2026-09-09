/*
 * Standalone, fake-only proof for the STDERR phase markers added to tests/_sw_gate_harness.js.
 *
 * Containment: this runs the harness SOURCE inside a vm context whose require/fs/path/process/console and
 * monotonic clock are all FAKES, against a SYNTHETIC service-worker stub. It NEVER reads or evaluates the
 * real src/ui/web/static/sw.js, never spawns the real harness or the application as a child, and touches no
 * network/app/auth. It proves the STDOUT JSON contract is unchanged (markers do not leak to stdout), that a
 * bounded phase marker is emitted to STDERR for every phase with within-process monotonic elapsed, and that
 * the synthetic gate (not the real one) drove the output. It does NOT prove the real gate's correctness or
 * any live/CI behavior. Run: node --test tests/sw_timeout_diagnostics.test.cjs
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Read ONLY the harness source text (not the real sw.js, and we never execute the harness as a child).
const harnessSource = fs.readFileSync(path.join(__dirname, '_sw_gate_harness.js'), 'utf8');

// SYNTHETIC service-worker stub — deliberately NOT the real sw.js. Just enough shape for the harness to
// eval + call a gate. Uses indexOf (not new URL) so it depends on nothing outside the vm context.
const SYNTH_SW = [
  "self.addEventListener('install', function () {});",
  "self.addEventListener('activate', function () {});",
  "self.addEventListener('fetch', function () {});",
  "function isShellAsset(request) {",
  "  return request.method === 'GET' && request.url.indexOf('/static/') !== -1;",
  "}",
].join('\n');

const EXPECTED_PHASES = [
  'start', 'load:begin', 'load:end', 'eval:begin', 'eval:end',
  'cases:begin', 'cases:end', 'output:begin', 'output:end',
];

function runHarnessWithFakes(opts) {
  opts = opts || {};
  const stdout = [];
  const stderr = []; // populated from the harness's marker sink: fs.writeSync(2, ...)
  let clock = 0n;
  let clockCalls = 0;
  let readReal = false;
  let streamWriteCalled = false; // tripwire: the corrected marker path must NOT touch the writable stream

  const fakeFs = {
    readFileSync(p) {
      if (typeof p === 'string' && p.indexOf('sw.js') !== -1) readReal = true; // must never happen here
      return SYNTH_SW;
    },
    writeSync(fd, s) {
      // SYNTHETIC: never writes a real descriptor. A synchronous write failure is thrown here (the harness
      // must catch it locally); markers to fd 2 are captured as the stderr stream would have received them.
      if (opts.brokenSink) throw new Error('synchronous write failure (EAGAIN/EBADF)');
      if (fd === 2) stderr.push(String(s));
      return String(s).length;
    },
  };
  const fakePath = { join() { return '<synthetic-sw-path>'; } };
  const fakeRequire = (name) => {
    if (name === 'fs') return fakeFs;
    if (name === 'path') return fakePath;
    throw new Error('blocked non-allowlisted require: ' + name); // fail closed
  };
  const fakeProcess = {
    hrtime: { bigint() {
      clockCalls += 1;
      if (opts.brokenClock) throw new Error('diagnostic clock unavailable'); // every read throws
      if (opts.brokenClockInit && clockCalls === 1) throw new Error('initial clock read unavailable'); // first only
      clock += 1000000n; return clock; // +1ms/call: within-process monotonic
    } },
    stderr: { write() { streamWriteCalled = true; return true; } }, // tripwire only; markers must not use this
  };
  const fakeConsole = { log(s) { stdout.push(String(s)); } };

  const context = vm.createContext({
    require: fakeRequire, process: fakeProcess, console: fakeConsole, __dirname: '<synthetic-dir>',
  });
  vm.runInContext(harnessSource, context, { filename: '_sw_gate_harness.js', timeout: 2000 });
  return { stdout, stderr, readReal, streamWriteCalled };
}

function parseMarkers(stderr) {
  // Each marker line: "[sw-gate-harness] <phase> +<ms>ms[ extra]"
  return stderr
    .join('')
    .split('\n')
    .filter((ln) => ln.indexOf('[sw-gate-harness]') === 0)
    .map((ln) => {
      const m = ln.match(/^\[sw-gate-harness\] (\S+) \+([0-9.]+)ms/);
      return m ? { phase: m[1], ms: parseFloat(m[2]) } : null;
    })
    .filter(Boolean);
}

test('a bounded phase marker is emitted to STDERR for every phase, in order', () => {
  const { stderr } = runHarnessWithFakes();
  const phases = parseMarkers(stderr).map((mk) => mk.phase);
  assert.deepEqual(phases, EXPECTED_PHASES);
});

test('marker durations are within-process monotonic elapsed (non-decreasing)', () => {
  const { stderr } = runHarnessWithFakes();
  const ms = parseMarkers(stderr).map((mk) => mk.ms);
  for (let i = 1; i < ms.length; i += 1) assert.ok(ms[i] >= ms[i - 1], 'elapsed must not go backwards');
  assert.ok(ms.length > 0 && ms.every((v) => Number.isFinite(v) && v >= 0));
});

test('STDOUT is the JSON contract only — exactly one line, valid JSON, no marker leakage', () => {
  const { stdout } = runHarnessWithFakes();
  assert.equal(stdout.length, 1, 'exactly one console.log (the JSON contract)');
  assert.equal(stdout[0].indexOf('[sw-gate-harness]'), -1, 'no marker text may appear on stdout');
  const results = JSON.parse(stdout[0]);
  assert.ok(Array.isArray(results) && results.length === 13);
  for (const r of results) {
    assert.ok('name' in r && 'got' in r && 'want' in r);
    assert.equal(typeof r.got, 'boolean');
  }
  const names = new Set(results.map((r) => r.name));
  for (const n of ['css', 'api', 'socketio', 'traversal', 'crossorigin', 'post_shell']) assert.ok(names.has(n));
});

test('the SYNTHETIC gate drove the results — the real sw.js is never read', () => {
  const { stdout, readReal } = runHarnessWithFakes();
  assert.equal(readReal, false, 'fs.readFileSync must never be asked for the real sw.js');
  const byName = Object.fromEntries(JSON.parse(stdout[0]).map((r) => [r.name, r.got]));
  // synthetic gate = GET && url contains '/static/': css true, post_shell(POST) false, api false, css-shell paths true
  assert.equal(byName.css, true);
  assert.equal(byName.post_shell, false); // POST -> false under the synthetic gate
  assert.equal(byName.api, false);
});

test('the marker path uses fd-2 fs.writeSync, never the writable stream (no deferred error possible)', () => {
  // Root finding 2: catching a synchronous stderr.write throw does not handle a later stream 'error' event.
  // The corrected harness writes via fs.writeSync(2, ...) and never touches process.stderr — demonstrated by
  // the tripwire staying false while markers are still captured on fd 2.
  const { stderr, streamWriteCalled } = runHarnessWithFakes();
  assert.equal(streamWriteCalled, false, 'process.stderr.write must never be called by the marker path');
  assert.ok(parseMarkers(stderr).length === EXPECTED_PHASES.length, 'markers still emitted via fd 2');
});

test('synchronous write failure is contained: stdout survives (finding 2)', () => {
  const { stdout, stderr } = runHarnessWithFakes({ brokenSink: true });
  assert.equal(stderr.length, 0, 'a throwing fs.writeSync captures nothing');
  assert.equal(stdout.length, 1, 'stdout JSON is still produced despite the synchronous write failure');
  assert.ok(JSON.parse(stdout[0]).length === 13);
});

test('an initially-unavailable clock SUPPRESSES markers (no fabricated elapsed) but keeps stdout (finding 1)', () => {
  // Root finding 1: _t0=0n let the first marker report the clock origin (+5000000002.0ms). Now the clock is
  // left undefined and markers are suppressed — even if later reads would succeed — while stdout is unchanged.
  const { stdout, stderr, streamWriteCalled } = runHarnessWithFakes({ brokenClockInit: true });
  assert.equal(stderr.length, 0, 'markers suppressed while the baseline is unavailable (no fabricated elapsed)');
  assert.equal(streamWriteCalled, false, 'no fallback to the writable stream either');
  assert.equal(stdout.length, 1, 'the gate outcome (stdout JSON) is preserved');
  assert.ok(JSON.parse(stdout[0]).length === 13);
});

test('a clock that throws on every read also suppresses markers and keeps stdout (finding 1)', () => {
  const { stdout, stderr } = runHarnessWithFakes({ brokenClock: true });
  assert.equal(stderr.length, 0, 'no markers when the clock is entirely unavailable');
  assert.equal(stdout.length, 1, 'stdout JSON is still produced');
  assert.ok(JSON.parse(stdout[0]).length === 13);
});
