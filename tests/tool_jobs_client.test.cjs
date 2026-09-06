const test = require('node:test');
const assert = require('node:assert/strict');
const { create } = require('../src/ui/web/static/tool_jobs_client.js');

const ID = 'a'.repeat(32), OTHER = 'b'.repeat(32);
const flush = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function status(id = ID, state = 'running', extra = {}) {
  return Object.assign({ job_id: id, tool: 'aircrack-ng', state, phase: 'extract', completed: 3,
    total: 10, error: '', log: ['extracting'], active: ['running', 'queued'].includes(state) }, extra);
}
function success(extra = {}) {
  return Object.assign({ schema_version: 1, tool: 'aircrack-ng', path: 'synthetic/tool.exe',
    version: '1.7', source: 'bundled', verification_method: 'sha256', state: 'succeeded' }, extra);
}
function harness(extra = {}) {
  const calls = [], timers = new Map(), observations = [];
  let now = 0, next = 1;
  const client = create(Object.assign({
    request(method, path, body, signal) {
      const pending = deferred();
      calls.push({ method, path, body, signal, ...pending });
      return pending.promise;
    },
    onChange(view) { observations.push(view); },
    setTimeout(fn, ms) { const id = next++; timers.set(id, { fn, at: now + ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
  }, extra));
  async function reply(index, code, body) {
    calls[index].resolve({ status: code, body });
    await flush();
  }
  async function tick(ms) {
    now += ms;
    for (const [id, timer] of [...timers]) {
      if (timer.at <= now) { timers.delete(id); timer.fn(); }
    }
    await flush();
  }
  return { client, calls, timers, observations, reply, tick };
}

test('one start, snapshot polling and a separate successful result; no synchronous fallback', async () => {
  const h = harness();
  const starting = h.client.start('aircrack-ng-1.7-win');
  assert.equal(await h.client.start('aircrack-ng-1.7-win'), false);
  await flush();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].path, '/api/crack/enable-bundled/async');
  assert.deepEqual(h.calls[0].body, { pack: 'aircrack-ng-1.7-win' });
  await h.reply(0, 202, { job_id: ID });
  await h.reply(1, 200, status());
  await starting;
  assert.equal(h.client.getState().snapshot.state, 'running');
  await h.tick(1000);
  await h.reply(2, 200, status(ID, 'succeeded'));
  assert.equal(h.calls[3].path, `/api/crack/job/${ID}/result`);
  await h.reply(3, 200, success({ ignored: 'not copied' }));
  const view = h.client.getState();
  assert.equal(view.snapshot.state, 'succeeded');
  assert.equal(view.resultStatus, 'available');
  assert.equal(view.result.ignored, undefined);
  assert.equal(h.calls.filter(c => c.method === 'POST').length, 1);
  assert.equal(h.timers.size, 0);
});

for (const code of [400, 401, 403, 409, 422, 503]) {
  test(`start rejection ${code} never triggers an automatic retry`, async () => {
    const h = harness();
    const starting = h.client.start('pack');
    await flush();
    await h.reply(0, code, { error: '<untrusted>' });
    await starting;
    assert.equal(h.client.getState().observation, 'rejected');
    assert.equal(h.client.getState().canStart, true);
    await h.tick(60000);
    assert.equal(h.calls.length, 1);
    assert.equal(JSON.stringify(h.client.getState()).includes('<untrusted>'), false);
  });
}

for (const kind of ['network', 'malformed-202', 'timeout', 'server-error']) {
  test(`lost start outcome (${kind}) blocks another start and never invents failure`, async () => {
    const h = harness({ timeoutMs: 100 });
    const starting = h.client.start('pack');
    await flush();
    if (kind === 'network') { h.calls[0].reject(new Error('offline')); await flush(); }
    else if (kind === 'malformed-202') await h.reply(0, 202, { job_id: '../foreign' });
    else if (kind === 'server-error') await h.reply(0, 500, {});
    else await h.tick(100);
    await starting;
    assert.equal(h.client.getState().observation, 'unknown');
    assert.equal(h.client.getState().snapshot, null);
    assert.equal(await h.client.start('pack'), false);
    await h.tick(60000);
    assert.equal(h.calls.length, 1);
  });
}

for (const code of [401, 403, 404, 409, 500]) {
  test(`observed success survives result response ${code}`, async () => {
    const h = harness();
    const watching = h.client.watch(ID);
    await flush();
    await h.reply(0, 200, status(ID, 'succeeded'));
    await h.reply(1, code, {});
    await watching;
    assert.equal(h.client.getState().snapshot.state, 'succeeded');
    assert.equal(h.client.getState().observation, 'complete');
    assert.equal(h.client.getState().resultStatus, 'unavailable');
    assert.equal(h.calls.filter(c => c.method === 'POST').length, 0);
    assert.equal(h.timers.size, 0);
  });
}

for (const body of [null, success({ tool: 'another-tool' }),
  { state: 'succeeded', result_status: 'unavailable', error_code: 'result_metadata_unavailable' }]) {
  test(`missing or mismatched success metadata is not an install failure: ${JSON.stringify(body)}`, async () => {
    const h = harness();
    const watching = h.client.watch(ID);
    await flush();
    await h.reply(0, 200, status(ID, 'succeeded'));
    await h.reply(1, 200, body);
    await watching;
    assert.equal(h.client.getState().snapshot.state, 'succeeded');
    assert.equal(h.client.getState().resultStatus, 'unavailable');
  });
}

test('cancel acknowledgment is not cancellation; duplicate clicks do not replay the POST', async () => {
  const h = harness();
  const watching = h.client.watch(ID);
  await flush(); await h.reply(0, 200, status()); await watching;
  const cancelling = h.client.cancel();
  assert.equal(await h.client.cancel(), false);
  await flush(); await h.reply(1, 200, { cancel_requested: true }); await cancelling;
  assert.equal(h.client.getState().cancel, 'requested');
  assert.equal(h.client.getState().snapshot.state, 'running');
  assert.equal(await h.client.cancel(), false);
  await h.tick(1000); await h.reply(2, 200, status(ID, 'cancelled'));
  assert.equal(h.client.getState().snapshot.state, 'cancelled');
  assert.equal(h.client.getState().cancel, 'closed');
  assert.equal(h.timers.size, 0);
});

test('late cancel acknowledgment cannot overwrite a concurrently completed job', async () => {
  const h = harness();
  const watching = h.client.watch(ID);
  await flush(); await h.reply(0, 200, status()); await watching;
  const cancelling = h.client.cancel();
  await flush(); await h.tick(1000);
  await h.reply(2, 200, status(ID, 'succeeded'));
  await h.reply(3, 200, success());
  await h.reply(1, 200, { cancel_requested: true }); await cancelling;
  assert.equal(h.client.getState().snapshot.state, 'succeeded');
  assert.equal(h.client.getState().cancel, 'closed');
});

test('cancel timeout keeps observing and does not retry cancellation', async () => {
  const h = harness({ timeoutMs: 100 });
  const watching = h.client.watch(ID);
  await flush(); await h.reply(0, 200, status()); await watching;
  const cancelling = h.client.cancel();
  await flush(); await h.tick(100); await cancelling;
  assert.equal(h.client.getState().cancel, 'unknown');
  assert.equal(h.client.getState().snapshot.state, 'running');
  await h.tick(900); await h.reply(2, 200, status(ID, 'cancelled'));
  assert.equal(h.client.getState().snapshot.state, 'cancelled');
  assert.equal(h.calls.filter(c => c.method === 'POST').length, 1);
});

test('status errors back off, pause after the bound and permit GET-only recovery', async () => {
  const h = harness({ maxFailures: 2 });
  const watching = h.client.watch(ID);
  await flush(); await h.reply(0, 500, {}); await watching;
  assert.equal(h.client.getState().observation, 'reconnecting');
  await h.tick(1999); assert.equal(h.calls.length, 1);
  await h.tick(1); await h.reply(1, 500, {});
  assert.equal(h.client.getState().observation, 'paused');
  assert.equal(h.timers.size, 0);
  const refreshing = h.client.refresh();
  await flush(); await h.reply(2, 200, status()); await refreshing;
  assert.equal(h.client.getState().observation, 'tracking');
  assert.ok(h.calls.every(c => c.method === 'GET'));
  h.client.dispose();
});

for (const code of [401, 403, 404]) {
  test(`status ${code} pauses observation without inventing a terminal state`, async () => {
    const h = harness();
    const watching = h.client.watch(ID);
    await flush(); await h.reply(0, code, {}); await watching;
    assert.equal(h.client.getState().observation, 'paused');
    assert.equal(h.client.getState().snapshot, null);
    assert.equal(h.client.getState().canStart, false);
    assert.equal(h.timers.size, 0);
  });
}

test('switching watched jobs ignores the old response and old finally cannot clear the new flight', async () => {
  const h = harness();
  const first = h.client.watch(ID);
  await flush();
  const second = h.client.watch(OTHER);
  await flush();
  assert.equal(h.calls[0].signal.aborted, true);
  await h.reply(0, 200, status(ID, 'succeeded'));
  assert.equal(await h.client.refresh(), false);
  assert.equal(h.calls.length, 2);
  await h.reply(1, 200, status(OTHER));
  await first; await second;
  assert.equal(h.client.getState().jobId, OTHER);
  assert.equal(h.client.getState().snapshot.state, 'running');
  h.client.dispose();
  await h.tick(60000);
  assert.equal(h.calls.length, 2);
});

test('observer disposal at start suppresses the POST entirely', async () => {
  let client;
  const h = harness({ onChange(view) { if (view.observation === 'starting') client.dispose(); } });
  client = h.client;
  assert.equal(await client.start('pack'), false);
  assert.equal(h.calls.length, 0);
  assert.equal(client.getState().observation, 'disposed');
});

test('observer disposal on success suppresses the subsequent metadata GET', async () => {
  let client;
  const h = harness({ onChange(view) { if (view.resultStatus === 'loading') client.dispose(); } });
  client = h.client;
  const watching = client.watch(ID);
  await flush(); await h.reply(0, 200, status(ID, 'succeeded')); await watching;
  assert.equal(h.calls.length, 1);
  assert.equal(client.getState().observation, 'disposed');
});

test('observer exceptions and mutations do not alter state or replace the log ring with accumulated lines', async () => {
  const h = harness({ onChange(view) {
    if (view.snapshot) view.snapshot.log.push('observer mutation');
    throw new Error('render failed');
  } });
  const watching = h.client.watch(ID);
  await flush(); await h.reply(0, 200, status(ID, 'running', {
    phase: '😀'.repeat(200), log: ['😀'.repeat(2000) + '…[truncated]'],
  })); await watching;
  assert.equal(h.client.getState().snapshot.log.length, 1);
  const refresh = h.client.refresh();
  await flush(); await h.reply(1, 200, status(ID, 'running', { log: ['replacement'] })); await refresh;
  assert.deepEqual(h.client.getState().snapshot.log, ['replacement']);
  const copy = h.client.getState(); copy.snapshot.log[0] = 'external mutation';
  assert.deepEqual(h.client.getState().snapshot.log, ['replacement']);
  h.client.dispose();
});

for (const malformed of [status(OTHER), status(ID, 'running', { active: false }),
  status(ID, 'running', { completed: Number.MAX_SAFE_INTEGER + 1 }),
  status(ID, 'running', { phase: 'x'.repeat(201) }), status(ID, 'running', { log: [null] })]) {
  test('invalid status preserves uncertainty and never triggers an install', async () => {
    const h = harness();
    const watching = h.client.watch(ID);
    await flush(); await h.reply(0, 200, malformed); await watching;
    assert.equal(h.client.getState().observation, 'paused');
    assert.equal(h.client.getState().snapshot, null);
    assert.equal(h.calls.length, 1);
  });
}

test('tool identity is pinned and failed/cancelled outcomes cannot regress on refresh', async () => {
  const h = harness();
  const watching = h.client.watch(ID);
  await flush(); await h.reply(0, 200, status()); await watching;
  const first = h.client.refresh();
  await flush(); await h.reply(1, 200, status(ID, 'running', { tool: 'another-tool' })); await first;
  assert.equal(h.client.getState().snapshot.tool, 'aircrack-ng');
  assert.equal(h.client.getState().observation, 'paused');
  const next = h.client.refresh();
  await flush(); await h.reply(2, 200, status(ID, 'failed')); await next;
  assert.equal(await h.client.refresh(), false);
  assert.equal(h.calls.length, 3);
});
