const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../src/ui/web/templates/dashboard.html'), 'utf8');
const scripts = [...template.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)];
assert.equal(scripts.length, 1);
const flush = async () => { for (let n = 0; n < 12; n++) await Promise.resolve(); };
const data = {device_count: 7, connected_count: 2, target_count: 0, status: 'healthy'};

function fixture({hidden = false, throwFetch = false} = {}) {
  const timers = new Map(), requests = [], listeners = new Map();
  const elements = Object.fromEntries(['device-count', 'connected-count', 'target-count', 'health-status']
    .map(id => [id, {textContent: 'initial'}]));
  let clock = 0, timerId = 0;
  const addTimer = (callback, delay, repeat) => {
    const id = ++timerId;
    timers.set(id, {callback, at: clock + delay, repeat});
    return id;
  };
  const document = {get hidden() {return hidden;}, getElementById: id => elements[id],
    addEventListener(name, callback) {const list = listeners.get(name) || []; list.push(callback); listeners.set(name, list);}};
  vm.runInNewContext(scripts[0][1], {
    document,
    fetch(url, options) {
      assert.equal(url, '/api/health'); assert.equal(options, undefined);
      if (throwFetch) {throwFetch = false; throw new Error('fetch failed synchronously');}
      return new Promise((resolve, reject) => requests.push({url, reject,
        respond(value = data) {resolve({json: async () => value});},
        headers(json) {resolve({json});}}));
    },
    setTimeout: (callback, delay) => addTimer(callback, delay, 0), clearTimeout: id => timers.delete(id),
    setInterval: (callback, delay) => addTimer(callback, delay, delay), clearInterval: id => timers.delete(id),
  }, {filename: 'dashboard.html inline health script', timeout: 1000});
  return {timers, requests, elements,
    async advance(ms) {
      const end = clock + ms;
      for (let count = 0; ; count++) {
        assert(count < 1000, 'finite fake-clock dispatch');
        const next = [...timers.entries()].sort((a, b) => a[1].at - b[1].at)[0];
        if (!next || next[1].at > end) break;
        const [id, timer] = next; clock = timer.at;
        if (timer.repeat) timer.at += timer.repeat; else timers.delete(id);
        timer.callback(); await flush();
      }
      clock = end; await flush();
    },
    async visibility(value) {
      hidden = value;
      for (const callback of listeners.get('visibilitychange') || []) callback();
      await flush();
    },
  };
}

test('visible startup preserves the five-second initial delay and metric rendering', async () => {
  const f = fixture();
  await f.advance(4999); assert.equal(f.requests.length, 0);
  await f.advance(1); assert.equal(f.requests.length, 1);
  f.requests[0].respond(); await flush();
  assert.deepEqual(Object.fromEntries(Object.entries(f.elements).map(([k, v]) => [k, v.textContent])),
    {'device-count': 7, 'connected-count': 2, 'target-count': 0, 'health-status': 'HEALTHY'});
  assert.equal(f.timers.size, 1);
});

test('initially hidden page does no polling and catches up once when visible', async () => {
  const f = fixture({hidden: true});
  await f.advance(20000); assert.equal(f.requests.length, 0); assert.equal(f.timers.size, 0);
  await f.visibility(false); assert.equal(f.requests.length, 1);
  await f.visibility(false); assert.equal(f.requests.length, 1);
});

test('slow health response never overlaps another request', async () => {
  const f = fixture(); await f.advance(5000);
  await f.advance(25000); assert.equal(f.requests.length, 1);
  f.requests[0].respond(); await flush();
  await f.advance(4999); assert.equal(f.requests.length, 1);
  await f.advance(1); assert.equal(f.requests.length, 2);
});

test('single request ownership includes pending JSON parsing', async () => {
  const f = fixture(); await f.advance(5000);
  let finishJson;
  f.requests[0].headers(() => new Promise(resolve => {finishJson = resolve;})); await flush();
  await f.advance(20000); assert.equal(f.requests.length, 1);
  finishJson(data); await flush();
  assert.equal(f.elements['health-status'].textContent, 'HEALTHY');
  await f.advance(5000); assert.equal(f.requests.length, 2);
});

test('hide cancels the pending timer and visible return performs one immediate catch-up', async () => {
  const f = fixture(); await f.advance(4000);
  await f.visibility(true); assert.equal(f.timers.size, 0);
  await f.advance(20000); assert.equal(f.requests.length, 0);
  await f.visibility(false); assert.equal(f.requests.length, 1); assert.equal(f.timers.size, 0);
  f.requests[0].respond(); await flush(); assert.equal(f.timers.size, 1);
  await f.visibility(false); await f.visibility(false); assert.equal(f.timers.size, 1);
  await f.advance(5000); assert.equal(f.requests.length, 2);
});

for (const reject of [false, true]) test('active '+(reject ? 'rejection' : 'success')+' while hidden cannot resurrect polling', async () => {
  const f = fixture(); await f.advance(5000); await f.visibility(true);
  reject ? f.requests[0].reject(new Error('offline')) : f.requests[0].respond(); await flush();
  assert.equal(f.timers.size, 0); await f.advance(20000); assert.equal(f.requests.length, 1);
  await f.visibility(false); assert.equal(f.requests.length, 2);
});

test('visible returns during active work coalesce into exactly one catch-up', async () => {
  const f = fixture(); await f.advance(5000);
  await f.visibility(true); await f.visibility(false); await f.visibility(false);
  await f.visibility(true); await f.visibility(false);
  assert.equal(f.requests.length, 1); assert.equal(f.timers.size, 0);
  f.requests[0].respond(); await flush(); assert.equal(f.requests.length, 2);
  await f.advance(20000); assert.equal(f.requests.length, 2);
  f.requests[1].respond(); await flush(); assert.equal(f.timers.size, 1);
  await f.advance(5000); assert.equal(f.requests.length, 3);
});

test('a final hide cancels deferred catch-up before active completion', async () => {
  const f = fixture(); await f.advance(5000);
  await f.visibility(true); await f.visibility(false); await f.visibility(true);
  f.requests[0].respond(); await flush();
  assert.equal(f.requests.length, 1); assert.equal(f.timers.size, 0);
  await f.advance(20000); assert.equal(f.requests.length, 1);
  await f.visibility(false); assert.equal(f.requests.length, 2);
});

for (const failure of ['fetch', 'json', 'render']) test(failure+' failure recovers with one later timer', async () => {
  const f = fixture(); await f.advance(5000);
  if (failure === 'fetch') f.requests[0].reject(new Error('offline'));
  if (failure === 'json') f.requests[0].headers(async () => {throw new Error('invalid JSON');});
  if (failure === 'render') f.requests[0].respond({...data, status: null});
  await flush(); assert.equal(f.timers.size, 1);
  await f.advance(5000); assert.equal(f.requests.length, 2);
  f.requests[1].respond(); await flush(); assert.equal(f.elements['health-status'].textContent, 'HEALTHY');
  assert.equal(f.timers.size, 1);
});

test('synchronous fetch failure releases ownership for the next scheduled poll', async () => {
  const f = fixture({throwFetch: true}); await f.advance(5000);
  assert.equal(f.requests.length, 0); assert.equal(f.timers.size, 1);
  await f.advance(5000); assert.equal(f.requests.length, 1);
  f.requests[0].respond(); await flush(); assert.equal(f.elements['health-status'].textContent, 'HEALTHY');
});

test('an already captured timer callback cannot start work after hiding', async () => {
  const f = fixture(), callback = [...f.timers.values()][0].callback;
  await f.visibility(true); callback(); await flush();
  assert.equal(f.requests.length, 0); assert.equal(f.timers.size, 0);
});
