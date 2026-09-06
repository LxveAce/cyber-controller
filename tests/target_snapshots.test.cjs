const test = require('node:test');
const assert = require('node:assert/strict');
const snapshots = require('../src/ui/web/static/target_snapshots.js');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const target = {target_type:'ble', mac:'02:00:00:00:00:01', ssid:'Fresh watch', rssi:-45};
async function flush() { for (let n=0;n<8;n++) await Promise.resolve(); }
function fixture() {
  const calls = [], states = [], rendered = [], timers = new Map();
  let clock = 0;
  const control = snapshots.create({
    load: signal => new Promise((resolve, reject) => calls.push({signal, resolve, reject})),
    onSnapshot: rows => rendered.push(rows), onStatus: state => states.push(state),
    setTimeout: callback => { timers.set(++clock, callback); return clock; },
    clearTimeout: id => timers.delete(id),
  });
  return {control, calls, states, rendered, timers};
}

test('routine polls coalesce without starving a slow successful request', async () => {
  const f = fixture();
  const first = f.control.refresh();
  for (let n=0;n<20;n++) assert.equal(f.control.refresh(), first);
  await flush(); assert.equal(f.calls.length, 1);
  f.calls[0].resolve([target]);
  assert.equal(await first, true);
  assert.equal(f.rendered[0][0].ssid, target.ssid);
  assert.equal(f.timers.size, 0);
});
test('explicit newer response wins; retired success cannot repaint', async () => {
  const f = fixture(); const old = f.control.refresh(); await flush();
  const current = f.control.refresh(true); await flush();
  assert.equal(f.calls[0].signal.aborted, true); assert.equal(await old, false);
  f.calls[1].resolve([target]); assert.equal(await current, true);
  f.calls[0].resolve([]); await flush();
  assert.equal(f.rendered.length, 1); assert.equal(f.states.at(-1), 'fresh');
});
test('latest failure does not make an older success current', async () => {
  const f = fixture(); f.control.refresh(); await flush();
  const current = f.control.refresh(true); await flush();
  f.calls[1].reject(new Error('offline')); assert.equal(await current, false);
  f.calls[0].resolve([target]); await flush();
  assert.equal(f.rendered.length, 0); assert.equal(f.states.at(-1), 'error');
});
test('timeout settles even when transport ignores abort; subsequent retry succeeds', async () => {
  const f = fixture(); const old = f.control.refresh(); await flush();
  [...f.timers.values()][0](); assert.equal(await old, false);
  assert.equal(f.calls[0].signal.aborted, true); assert.equal(f.timers.size, 0);
  const next = f.control.refresh(); await flush(); f.calls[1].resolve([target]); await next;
  f.calls[0].reject(new Error('late')); await flush();
  assert.equal(f.rendered.length, 1); assert.equal(f.states.at(-1), 'fresh');
});
test('suspend invalidates and settles pending work; resume uses the same owner', async () => {
  const f = fixture(); const old = f.control.refresh(); await flush(); f.control.suspend();
  assert.equal(await old, false); assert.equal(f.timers.size, 0);
  assert.equal(await f.control.refresh(), false); assert.equal(f.calls.length, 1);
  const next = f.control.resume(); await flush();
  f.calls[0].resolve([target]); await flush(); assert.equal(f.rendered.length, 0);
  f.calls[1].resolve([]); assert.equal(await next, true); assert.equal(f.rendered.length, 1);
});
test('a synchronous transport failure releases the next refresh', async () => {
  const states = [];
  const c = snapshots.create({load:()=>{throw new Error('sync');},onSnapshot:()=>assert.fail(),onStatus:s=>states.push(s)});
  assert.equal(await c.refresh(), false); assert.equal(await c.refresh(), false);
  assert.deepEqual(states,['loading','error','loading','error']);
});
for (const invalid of [null, {}, [null], [[target]], [{...target,encryption:{}}], [{...target,rssi:'-45'}], [{...target,rssi:true}], [{...target,channel:[]}], [{...target,device_source:4}], [{...target,last_seen:{}}], Array(5001).fill(target)]) {
  test('invalid snapshot preserves the entire last-good snapshot: '+JSON.stringify(invalid).slice(0,65), async () => {
    const f=fixture(); let p=f.control.refresh(); await flush(); f.calls[0].resolve([target]); await p;
    p=f.control.refresh(); await flush(); f.calls[1].resolve(invalid); assert.equal(await p,false);
    assert.equal(f.rendered.length,1); assert.equal(f.states.at(-1),'error');
  });
}
for (const status of [401,403]) test('auth '+status+' clears retained rows', async () => {
  const f=fixture(); let p=f.control.refresh(); await flush(); f.calls[0].resolve([target]); await p;
  p=f.control.refresh(); await flush(); f.calls[1].reject({status}); await p;
  assert.deepEqual(f.rendered.at(-1),[]); assert.equal(f.states.at(-1),'unauthorized');
});
test('snapshot copies displayed fields, accepts full pool and ignores unused extra objects', () => {
  const input = {...target, extra:{encryption:{}}}; const rows=snapshots.validate(Array(5000).fill(input));
  input.ssid='Changed'; assert.equal(rows.length,5000); assert.equal(rows[0].ssid,'Fresh watch');
  assert.equal(rows[0].extra,undefined);
});
test('unknown signal values remain unknown without string/Boolean coercion', () => {
  for (const x of [0,-0,null,undefined,NaN,Infinity,-Infinity,'-45',false,true,{}]) assert.equal(snapshots.bleSignal(x),null);
  for (const x of [-127,-45,-1,1]) assert.equal(snapshots.bleSignal(x),x);
});

test('exact Reform render keeps unknown BLE out of strongest and full-height bars', () => {
  const source=fs.readFileSync(path.join(__dirname,'../src/ui/web/static/reform.js'),'utf8');
  const start=source.indexOf('  function tile('), end=source.indexOf('  // ── live serial',start);
  assert(start>=0 && end>start);
  const elements=Object.fromEntries(['hunt-ble-tiles','hunt-ble-rows','hunt-targets-rows'].map(id=>[id,{innerHTML:''}]));
  const bars={innerHTML:'',parentElement:{hidden:false}};
  const context=vm.createContext({window:{CCTargetSnapshots:snapshots,CCTargetSelection:require('../src/ui/web/static/target_selection.js')},document:{getElementById:id=>elements[id]||null,querySelector:()=>bars},
    esc:s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])),ageOf:()=> 'now'});
  vm.runInContext(source.slice(start,end),context);
  context.renderHunt([target,{...target,mac:'02:00:00:00:00:02',ssid:'<img src=x>',rssi:0}]);
  assert.match(elements['hunt-ble-tiles'].innerHTML,/>-45</);
  assert.doesNotMatch(elements['hunt-ble-tiles'].innerHTML,/>0</);
  assert.match(elements['hunt-ble-rows'].innerHTML,/&lt;img src=x&gt;/);
  assert.match(elements['hunt-ble-rows'].innerHTML,/>—</);
  assert.match(elements['hunt-targets-rows'].innerHTML,/>—</);
  assert.match(bars.innerHTML,/signal unknown/); assert.match(bars.innerHTML,/height:0%/);
  assert.doesNotMatch(bars.innerHTML,/<img|height:100%/);
  context.renderHunt([{...target,rssi:null}]); assert.match(elements['hunt-ble-tiles'].innerHTML,/>—</);
});

test('bounded transport rejects an over-limit stream and cancels it', async () => {
  const oldFetch=global.fetch, oldWindow=global.window; let cancelled=false;
  global.window={CSRF_TOKEN:'fixture'};
  global.fetch=async (url,options)=>{
    assert.equal(url,'/api/targets'); assert.equal(options.credentials,'same-origin'); assert.equal(options.redirect,'error');
    return {ok:true,body:new ReadableStream({pull(c){c.enqueue(new Uint8Array(snapshots.MAX_BYTES+1));},cancel(){cancelled=true;}})};
  };
  try { await assert.rejects(snapshots.transport(new AbortController().signal),/too large/); assert.equal(cancelled,true); }
  finally {global.fetch=oldFetch;global.window=oldWindow;}
});
test('bounded transport decodes UTF-8 across chunks and rejects invalid UTF-8', async () => {
  const oldFetch=global.fetch, oldWindow=global.window; global.window={};
  try {
    const bytes=new TextEncoder().encode(JSON.stringify([{...target,ssid:'Café'}]));
    global.fetch=async()=>({ok:true,body:new ReadableStream({start(c){for(const b of bytes)c.enqueue(Uint8Array.of(b));c.close();}})});
    assert.equal((await snapshots.transport(new AbortController().signal))[0].ssid,'Café');
    global.fetch=async()=>({ok:true,body:new ReadableStream({start(c){c.enqueue(Uint8Array.of(255));c.close();}})});
    await assert.rejects(snapshots.transport(new AbortController().signal));
  } finally {global.fetch=oldFetch;global.window=oldWindow;}
});

test('snapshot consumer replacement owns the next status', async () => {
  const calls=[],states=[];let replacement;
  const c=snapshots.create({load:()=>new Promise(resolve=>calls.push(resolve)),
    onSnapshot:()=>{if(!replacement)replacement=c.refresh(true);},onStatus:s=>states.push(s)});
  const old=c.refresh();await flush();calls[0]([target]);assert.equal(await old,false);await flush();
  assert.equal(states.at(-1),'loading');calls[1]([]);assert.equal(await replacement,true);
});
test('snapshot consumer failure settles its request and allows retry', async () => {
  const states=[];let fail=true;
  const c=snapshots.create({load:()=>Promise.resolve([target]),onSnapshot:()=>{if(fail)throw new Error('render');},onStatus:s=>states.push(s)});
  assert.equal(await c.refresh(),false);assert.equal(states.at(-1),'error');fail=false;
  assert.equal(await c.refresh(),true);assert.equal(states.at(-1),'fresh');
});
test('abort retires an acquired stream even when fetch ignores the signal', async () => {
  const oldFetch=global.fetch,oldWindow=global.window;global.window={};let cancelled=0;
  const body=new ReadableStream({cancel(){cancelled++;}});
  global.fetch=async()=>({ok:true,body});
  try {
    const abort=new AbortController(),pending=snapshots.transport(abort.signal);
    await flush();assert.equal(body.locked,true);abort.abort();await assert.rejects(pending);
    assert.equal(cancelled,1);assert.equal(body.locked,false);
  }finally{global.fetch=oldFetch;global.window=oldWindow;}
});
test('late headers and rejected responses cancel their unread bodies', async () => {
  const oldFetch=global.fetch,oldWindow=global.window;global.window={};
  try {
    for(const retired of [false,true]){
      let headers,cancelled=0;
      const body=new ReadableStream({cancel(){cancelled++;}});
      global.fetch=()=>new Promise(resolve=>headers=resolve);
      const abort=new AbortController(),pending=snapshots.transport(abort.signal);
      if(retired)abort.abort();headers({ok:retired,status:401,body});
      await assert.rejects(pending);assert.equal(cancelled,1);assert.equal(body.locked,false);
    }
  }finally{global.fetch=oldFetch;global.window=oldWindow;}
});

for(const failingState of ['loading','fresh']) test('a throwing '+failingState+' observer does not own the request',async()=>{
  let count=0;
  const c=snapshots.create({load:()=>Promise.resolve([target]),onSnapshot:()=>count++,onStatus:state=>{if(state===failingState)throw new Error('status display');}});
  assert.equal(await c.refresh(),true);assert.equal(await c.refresh(),true);assert.equal(count,2);
});
test('replacement during loading owns the only remaining request and deadline',async()=>{
  const timers=new Map(),calls=[];let id=0,replaced=false,replacement;
  const c=snapshots.create({load:()=>new Promise(resolve=>calls.push(resolve)),onSnapshot:()=>{},
    onStatus:state=>{if(state==='loading'&&!replaced){replaced=true;replacement=c.refresh(true);}},
    setTimeout:cb=>{timers.set(++id,cb);return id;},clearTimeout:id=>timers.delete(id)});
  assert.equal(await c.refresh(),false);await flush();assert.equal(calls.length,1);assert.equal(timers.size,1);
  calls[0]([]);assert.equal(await replacement,true);assert.equal(timers.size,0);
});
