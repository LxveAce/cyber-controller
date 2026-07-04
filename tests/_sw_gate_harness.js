/*
 * Behavioral harness for the service-worker cache gate. Loads the REAL src/ui/web/static/sw.js (real JS
 * URL parser + the actual isShellAsset function) and runs it against adversarial URLs, printing JSON. The
 * Python test (test_web_pwa.py::test_sw_gate_behavioral) asserts the results — this catches a real gate
 * regression (e.g. an inverted return) that the lexical substring tests cannot.
 */
const fs = require('fs');
const path = require('path');

// Minimal ServiceWorkerGlobalScope stub so sw.js loads (its addEventListener callbacks never fire here).
globalThis.self = {
  location: { origin: 'https://cc.local' },
  addEventListener() {},
  skipWaiting() {},
  clients: { claim() {} },
};

const swPath = path.join(__dirname, '..', 'src', 'ui', 'web', 'static', 'sw.js');
let src = fs.readFileSync(swPath, 'utf8');
// Export the in-scope gate so we can call it regardless of eval const/let scoping.
src += '\nglobalThis.__isShellAsset = isShellAsset;';
eval(src); // eslint-disable-line no-eval

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
const results = cases.map((c) => ({ name: c.name, got: gate({ method: c.method, url: c.url }), want: c.want }));
console.log(JSON.stringify(results));
