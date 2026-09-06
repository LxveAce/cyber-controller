const nodeTest = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const snapshots = require('../src/ui/web/static/target_snapshots.js');
const test = (name, fn) => nodeTest(name, {timeout: 2000}, fn);

const source = fs.readFileSync(path.join(__dirname, '../src/ui/web/static/target_snapshots.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const target = {target_type: 'ble', mac: '02:00:00:00:00:01', ssid: 'Inert target', rssi: -50};

function fixture(options = {}, api = snapshots) {
  let nextTimer = 0;
  const calls = [], delivered = [], states = [], timers = new Map();
  const control = api.create({
    load: signal => new Promise((resolve, reject) => calls.push({signal, resolve, reject})),
    onSnapshot: (rows, event) => delivered.push({rows, event}),
    onStatus: state => states.push(state),
    setTimeout: fn => { timers.set(++nextTimer, fn); return nextTimer; },
    clearTimeout: id => timers.delete(id),
    ...options,
  });
  return {control, calls, delivered, states, timers};
}

function transportModule(fetch) {
  const module = {exports: {}};
  vm.runInNewContext(source, {module, fetch, TextDecoder, AbortController, performance,
    window: {CSRF_TOKEN: 'inert-fixture'}, setTimeout, clearTimeout});
  return module.exports;
}

test('abort callback replacement settles the superseded invocation and preserves the newest slot', async () => {
  const f = fixture();
  let newest;
  const first = f.control.refresh();
  await flush();
  f.calls[0].signal.addEventListener('abort', () => { newest = f.control.refresh(true); }, {once: true});
  try {
    const superseded = f.control.refresh(true);
    assert.equal(await first, false);
    assert.equal(await superseded, false);
    await flush();
    assert.equal(f.calls.length, 2);
    assert.equal(f.control.refresh(), newest);
    assert.equal(f.timers.size, 1);
    f.calls[1].resolve([target]);
    assert.equal(await newest, true);
    assert.equal(f.delivered.length, 1);
    assert.equal(f.states.at(-1), 'fresh');
    assert.equal(f.timers.size, 0);
    const retry = f.control.refresh();
    await flush();
    f.calls[2].resolve([]);
    assert.equal(await retry, true);
    assert.equal(f.timers.size, 0);
    f.calls[0].reject({status: 401});
    await flush();
    assert.equal(f.delivered.length, 2);
    assert.equal(f.states.at(-1), 'fresh');
  } finally { f.control.suspend(); }
});

test('actual stream cancellation reentry keeps the inner refresh and releases the old reader', async () => {
  let f, newest, cancelled = 0;
  const streams = [], bodies = [];
  const api = transportModule(async () => {
    const body = new ReadableStream({
      start(controller) { streams.push(controller); },
      cancel() { cancelled++; if (cancelled === 1) newest = f.control.refresh(true); },
    });
    bodies.push(body);
    return {ok: true, body};
  });
  f = fixture({load: api.transport}, api);
  const first = f.control.refresh();
  await flush();
  try {
    const superseded = f.control.refresh(true);
    assert.equal(await first, false);
    assert.equal(await superseded, false);
    await flush();
    assert.equal(streams.length, 2);
    assert.equal(cancelled, 1);
    assert.equal(bodies[0].locked, false);
    assert.equal(f.control.refresh(), newest);
    streams[1].enqueue(new TextEncoder().encode(JSON.stringify([target])));
    streams[1].close();
    assert.equal(await newest, true);
    assert.equal(bodies[1].locked, false);
    assert.equal(f.timers.size, 0);
    assert.equal(f.delivered.length, 1);
  } finally { f.control.suspend(); }
});

for (const resumeInsideCallback of [false, true]) {
  test('replacement cancellation can suspend' + (resumeInsideCallback ? ' and resume synchronously' : ' until later resume'), async () => {
    const f = fixture();
    let resumed;
    const first = f.control.refresh();
    await flush();
    f.calls[0].signal.addEventListener('abort', () => {
      f.control.suspend();
      if (resumeInsideCallback) resumed = f.control.resume();
    }, {once: true});
    try {
      const superseded = f.control.refresh(true);
      assert.equal(await first, false);
      assert.equal(await superseded, false);
      await flush();
      if (!resumeInsideCallback) {
        assert.equal(f.calls.length, 1);
        assert.equal(f.timers.size, 0);
        assert.equal(await f.control.refresh(), false);
        resumed = f.control.resume();
      }
      await flush();
      assert.equal(f.calls.length, 2);
      assert.equal(f.timers.size, 1);
      assert.equal(f.control.resume(), resumed);
      f.calls[1].resolve([target]);
      assert.equal(await resumed, true);
      assert.equal(f.timers.size, 0);
      assert.equal(f.delivered.length, 1);
    } finally { f.control.suspend(); }
  });
}

test('ordinary polling and loading-observer replacement keep one request and one timer', async () => {
  let f, newest, replaceOnce = true;
  f = fixture({onStatus: state => {
    if (state === 'loading' && replaceOnce) { replaceOnce = false; newest = f.control.refresh(true); }
  }});
  try {
    assert.equal(await f.control.refresh(), false);
    await flush();
    assert.equal(f.calls.length, 1);
    for (let n = 0; n < 10; n++) assert.equal(f.control.refresh(), newest);
    assert.equal(f.timers.size, 1);
    f.calls[0].resolve([]);
    assert.equal(await newest, true);
    assert.equal(f.timers.size, 0);
  } finally { f.control.suspend(); }
});

test('loading-observer suspension starts no transport and resume can retry', async () => {
  let f, suspendOnce = true;
  f = fixture({onStatus: state => {
    if (state === 'loading' && suspendOnce) { suspendOnce = false; f.control.suspend(); }
  }});
  try {
    assert.equal(await f.control.refresh(), false);
    await flush();
    assert.equal(f.calls.length, 0);
    assert.equal(f.timers.size, 0);
    const resumed = f.control.resume();
    await flush();
    f.calls[0].resolve([]);
    assert.equal(await resumed, true);
    assert.equal(f.timers.size, 0);
  } finally { f.control.suspend(); }
});

test('direct suspension lets an abort callback resume without coalescing onto the retired request', async () => {
  const f = fixture();
  let resumed;
  const first = f.control.refresh();
  await flush();
  const oldTimer = [...f.timers.values()][0];
  f.calls[0].signal.addEventListener('abort', () => { resumed = f.control.resume(); }, {once: true});
  try {
    f.control.suspend();
    assert.equal(await first, false);
    await flush();
    assert.notEqual(resumed, first);
    assert.equal(f.calls.length, 2);
    assert.equal(f.control.refresh(), resumed);
    assert.equal(f.timers.size, 1);
    oldTimer();
    f.calls[0].reject({status: 403});
    await flush();
    assert.equal(f.calls[1].signal.aborted, false);
    assert.equal(f.delivered.length, 0);
    f.calls[1].resolve([target]);
    assert.equal(await resumed, true);
    assert.equal(f.delivered.length, 1);
    assert.equal(f.states.at(-1), 'fresh');
    assert.equal(f.timers.size, 0);
  } finally { f.control.suspend(); }
});

test('nested suspend and resume during loading preserve only the newest intent', async () => {
  let f, resumed, newest, loadNotifications = 0;
  f = fixture({onStatus: state => {
    if (state === 'loading' && ++loadNotifications === 2) {
      f.control.suspend();
      newest = f.control.resume();
    }
  }});
  const first = f.control.refresh();
  await flush();
  f.calls[0].signal.addEventListener('abort', () => { resumed = f.control.resume(); }, {once: true});
  try {
    f.control.suspend();
    assert.equal(await first, false);
    assert.equal(await resumed, false);
    await flush();
    assert.equal(f.calls.length, 2);
    assert.equal(f.control.refresh(), newest);
    assert.equal(f.timers.size, 1);
    f.calls[0].resolve([]);
    await flush();
    assert.equal(f.delivered.length, 0);
    f.calls[1].resolve([target]);
    assert.equal(await newest, true);
    assert.equal(f.delivered.length, 1);
    assert.equal(f.timers.size, 0);
  } finally { f.control.suspend(); }
});
