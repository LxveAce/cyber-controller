/*
 * Behavioral harness for the service-worker cache gate. Loads the REAL src/ui/web/static/sw.js (real JS
 * URL parser + the actual isShellAsset function) and runs it against adversarial URLs, printing JSON. The
 * Python test (test_web_pwa.py::test_sw_gate_behavioral) asserts the results — this catches a real gate
 * regression (e.g. an inverted return) that the lexical substring tests cannot.
 */
const fs = require('fs');
const path = require('path');

// Bounded within-process observability: monotonic phase/duration markers written to STDERR (fd 2) ONLY. They never
// touch the STDOUT JSON contract the Python test parses, so a passing run is byte-for-byte unchanged on stdout.
// They exist so a future timeout/failure is localizable to a phase. Note the limits (do not over-read them):
// a MISSING marker does not prove the child never started, and the final marker does not prove stdout flushed
// or the process exited. Durations are elapsed within THIS process (never compared to another process' clock).
// Best effort + self-contained: if the INITIAL clock read is unavailable the clock stays undefined and markers
// are SUPPRESSED (never a fabricated zero-origin elapsed); each marker guards a SYNCHRONOUS fs.writeSync to fd 2,
// so a diagnostic write failure stays in its own catch: no writable-stream deferred error event, no global
// handler. Error handling, not a guarantee that OS I/O cannot stall or that a whole line is written.
let _t0;
try { _t0 = process.hrtime.bigint(); } catch (e) { /* clock unavailable -> markers suppressed below */ }
function _mark(phase, extra) {
  if (_t0 === undefined) return;   // no valid baseline -> suppress (never a fabricated zero-origin elapsed)
  try {
    const ms = Number(process.hrtime.bigint() - _t0) / 1e6;
    fs.writeSync(2, '[sw-gate-harness] ' + phase + ' +' + ms.toFixed(1) + 'ms'
      + (extra ? ' ' + extra : '') + '\n');
  } catch (e) { /* a diagnostic failure (clock or write) must not change the gate outcome */ }
}
_mark('start');

// Minimal ServiceWorkerGlobalScope stub so sw.js loads (its addEventListener callbacks never fire here).
globalThis.self = {
  location: { origin: 'https://cc.local' },
  addEventListener() {},
  skipWaiting() {},
  clients: { claim() {} },
};

const swPath = path.join(__dirname, '..', 'src', 'ui', 'web', 'static', 'sw.js');
_mark('load:begin');
let src = fs.readFileSync(swPath, 'utf8');
_mark('load:end', 'bytes=' + src.length);
// Export the in-scope gate so we can call it regardless of eval const/let scoping.
src += '\nglobalThis.__isShellAsset = isShellAsset;';
_mark('eval:begin');
eval(src); // eslint-disable-line no-eval
_mark('eval:end');

const gate = globalThis.__isShellAsset;
const o = self.location.origin;
const cases = [
  { name: 'css',          url: o + '/static/style.css',            method: 'GET',  want: true  },
  { name: 'manifest',     url: o + '/manifest.webmanifest',        method: 'GET',  want: true  },
  { name: 'icon',         url: o + '/static/icons/ace-192.png',    method: 'GET',  want: true  },
  { name: 'api',          url: o + '/api/devices',                 method: 'GET',  want: false },
  { name: 'api_qs',       url: o + '/api/devices?x=1',             method: 'GET',  want: false },
  { name: 'socketio',     url: o + '/socket.io/?EIO=4',            method: 'GET',  want: false },
  { name: 'dashboard',    url: o + '/',                            method: 'GET',  want: false },
  { name: 'terminal',     url: o + '/terminal/COM5',               method: 'GET',  want: false },
  { name: 'nodes',        url: o + '/nodes',                       method: 'GET',  want: false },
  { name: 'traversal',    url: o + '/static/../api/devices',       method: 'GET',  want: false },
  { name: 'enc_traversal',url: o + '/%2e%2e/api/devices',          method: 'GET',  want: false },
  { name: 'crossorigin',  url: 'https://evil.example/static/style.css', method: 'GET', want: false },
  { name: 'post_shell',   url: o + '/static/style.css',            method: 'POST', want: false },
];
_mark('cases:begin', 'n=' + cases.length);
const results = cases.map((c) => ({ name: c.name, got: gate({ method: c.method, url: c.url }), want: c.want }));
_mark('cases:end');
_mark('output:begin');
console.log(JSON.stringify(results));
_mark('output:end');
