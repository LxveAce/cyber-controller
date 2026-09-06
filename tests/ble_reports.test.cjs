const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const reports = require('../src/ui/web/static/ble_reports.js');
const targets = require('../src/ui/web/static/target_snapshots.js');
const source = fs.readFileSync(path.join(__dirname, '../src/ui/web/static/ble_reports.js'), 'utf8');
const row = extra => ({ observation_id:'1', label:'Inert report', rssi:0, reported_index:null, format:'live',
  label_truncated:false, addressable:false, device_source:'', connection_epoch:'1', scan_epoch:'1',
  observed_at:'2026-09-06T12:00:00.123456+00:00', ...extra });
const envelope = observations => ({available:true, observations:observations || [row()]});
const flush = async () => { for(let i=0;i<12;i++) await Promise.resolve(); };
const detached = value => JSON.parse(JSON.stringify(value));
function fixture(api=reports, overrides={}) {
  const calls=[], states=[], values=[], timers=new Map(); let id=0, clock=0;
  const options={ load:signal=>new Promise((resolve,reject)=>calls.push({signal,resolve,reject})),
    onReports:(value,meta)=>values.push({value,meta}), onSnapshot:(value,meta)=>values.push({value,meta}),
    onStatus:state=>states.push(state), setTimeout:(fn,ms)=>{timers.set(++id,{fn,ms});return id;},
    clearTimeout:key=>timers.delete(key), now:()=>clock, ...overrides };
  const control=api.create(options);
  return {control,options,calls,states,values,timers,clock:value=>{clock=value;},
    expire:()=>{clock=15000;for(const timer of [...timers.values()])timer.fn();}};
}
const currentTarget=[{target_type:'ble',mac:'02:00:00:00:00:01',ssid:'Inert'}];
// Common ownership cases deliberately exercise both readers without sharing their data contracts.
for(const [name,api,data] of [['reports',reports,envelope()],['targets',targets,currentTarget]]) {
  test(name+': polls coalesce; forced refresh retires old success and auth',async()=>{
    const f=fixture(api);const old=f.control.refresh();await flush();
    assert.equal(f.control.refresh(),old);const next=f.control.refresh(true);await flush();
    assert.equal(await old,false);assert(f.calls[0].signal.aborted);
    f.calls[1].resolve(data);assert.equal(await next,true);f.calls[0].reject({status:401});await flush();
    assert.equal(f.values.length,1);assert.equal(f.states.at(-1),'fresh');assert.equal(f.timers.size,0);
  });
  test(name+': ignored-abort timeout frees slot and retry succeeds',async()=>{
    const f=fixture(api);const old=f.control.refresh();await flush();f.expire();
    assert.equal(await old,false);assert.equal(f.timers.size,0);assert(f.calls[0].signal.aborted);
    const next=f.control.refresh();await flush();f.calls[1].resolve(data);assert.equal(await next,true);
    f.calls[0].resolve(data);await flush();assert.equal(f.values.length,1);
  });
  test(name+': suspend/resume stays single-flight and ignores retired auth',async()=>{
    const f=fixture(api);const old=f.control.refresh();await flush();f.control.suspend();f.control.suspend();
    assert.equal(await old,false);assert.equal(await f.control.refresh(),false);assert.equal(f.timers.size,0);
    const next=f.control.resume();assert.equal(f.control.resume(),next);await flush();
    f.calls[0].reject({status:403});await flush();assert.equal(f.values.length,0);
    f.calls[1].resolve(data);assert.equal(await next,true);assert.equal(f.values.length,1);
  });
  test(name+': loading reentry suspend allocates no timer or transport',async()=>{
    let f;f=fixture(api,{onStatus:state=>{if(state==='loading')f.control.suspend();}});
    assert.equal(await f.control.refresh(),false);await flush();assert.equal(f.calls.length,0);assert.equal(f.timers.size,0);
  });
  test(name+': callback replacement cannot publish old fresh status',async()=>{
    let f,next,reentered=false;
    const callback=()=>{if(!reentered){reentered=true;next=f.control.refresh(true);}};
    f=fixture(api,{onReports:callback,onSnapshot:callback});const old=f.control.refresh();await flush();
    f.calls[0].resolve(data);assert.equal(await old,false);await flush();assert.equal(f.states.at(-1),'loading');
    f.calls[1].resolve(data);assert.equal(await next,true);assert.equal(f.timers.size,0);
  });
  test(name+': throwing data/status observers settle and permit retry',async()=>{
    let fails=true;const callback=()=>{if(fails)throw Error('inert observer');};
    const f=fixture(api,{onReports:callback,onSnapshot:callback,onStatus:()=>{throw Error('status');}});
    let p=f.control.refresh();await flush();f.calls[0].resolve(data);assert.equal(await p,false);assert.equal(f.timers.size,0);
    fails=false;p=f.control.refresh();await flush();f.calls[1].resolve(data);assert.equal(await p,true);assert.equal(f.timers.size,0);
  });
}

test('dependencies captured; unavailable read succeeds; observer mutation cannot relabel availability',async()=>{
  const f=fixture(reports,{onReports:value=>{value.available=true;}});
  f.options.onReports=()=>assert.fail();f.options.load=()=>assert.fail();
  const p=f.control.refresh();await flush();f.calls[0].resolve({available:false,observations:[]});
  assert.equal(await p,true);assert.equal(f.states.at(-1),'unavailable');assert.equal(f.timers.size,0);
});
for(const status of [401,403])test('current auth '+status+' clears reports using report-only metadata',async()=>{
  const f=fixture();const p=f.control.refresh();await flush();f.calls[0].reject({status});assert.equal(await p,false);
  assert.deepEqual(f.values[0].value,{available:false,observations:[]});assert.equal(f.values[0].meta.kind,'auth-loss');
  assert.equal(f.states.at(-1),'unauthorized');assert.equal(f.timers.size,0);
});
test('late result with a delayed timer is rejected before publication',async()=>{
  const f=fixture();const p=f.control.refresh();await flush();f.clock(15000);f.calls[0].resolve(envelope());
  assert.equal(await p,false);assert.equal(f.values.length,0);assert.equal(f.timers.size,0);assert(f.calls[0].signal.aborted);
});
test('validation work cannot overrun deadline before publication admission',async()=>{
  const f=fixture();const data=envelope();Object.defineProperty(data.observations[0],'label',{get(){f.clock(16000);return 'inert';}});
  const p=f.control.refresh();await flush();f.calls[0].resolve(data);assert.equal(await p,false);assert.equal(f.values.length,0);
});
test('loading observer cannot consume full deadline then start transport',async()=>{
  let f;f=fixture(reports,{onStatus:s=>{if(s==='loading')f.clock(15000);}});
  assert.equal(await f.control.refresh(),false);await flush();assert.equal(f.calls.length,0);assert.equal(f.timers.size,0);
});
test('loading replacement starts only the new request and timer',async()=>{
  let f,next,reentered=false;
  f=fixture(reports,{onStatus:s=>{if(s==='loading'&&!reentered){reentered=true;next=f.control.refresh(true);}}});
  assert.equal(await f.control.refresh(),false);await flush();assert.equal(f.calls.length,1);assert.equal(f.timers.size,1);
  f.calls[0].resolve(envelope());assert.equal(await next,true);assert.equal(f.timers.size,0);
});
test('data callback suspension settles false and subsequent resume remains usable',async()=>{
  let f,once=true;f=fixture(reports,{onReports:()=>{if(once){once=false;f.control.suspend();}}});
  const p=f.control.refresh();await flush();f.calls[0].resolve(envelope());assert.equal(await p,false);assert.equal(f.timers.size,0);
  const next=f.control.resume();await flush();f.calls[1].resolve(envelope());assert.equal(await next,true);
});
test('unavailable status failure cannot turn a completed read into failure',async()=>{
  const f=fixture(reports,{onStatus:s=>{if(s==='unavailable')throw Error('status');}});
  const p=f.control.refresh();await flush();f.calls[0].resolve({available:false,observations:[]});assert.equal(await p,true);assert.equal(f.timers.size,0);
});
test('validation preserves literal repeated labels, explicit zero, decimal strings and detached data',()=>{
  const input=envelope([row({label:'02:00:00:00:00:01',observation_id:'9'.repeat(64)}),row({label:'02:00:00:00:00:01'})]);
  input.observations[0].extra={secret:'ignored'};const copy=reports.validate(input);input.observations[0].label='mutated';
  assert.equal(copy.observations.length,2);assert.equal(copy.observations[0].label,'02:00:00:00:00:01');
  assert.equal(copy.observations[0].rssi,0);assert.equal(typeof copy.observations[0].observation_id,'string');
  assert.equal(copy.observations[0].extra,undefined);assert.equal(copy.observations[0].mac,undefined);assert.equal(copy.observations[0].timestamp,undefined);
});
test('maximum Unicode code points and empty sources are admitted without UTF16 false rejection',()=>{
  const data=envelope([row({label:'😀'.repeat(256),device_source:'😀'.repeat(512)}),row()]);
  assert.equal(reports.validate(data).observations[0].label.length,512);
  assert.equal(reports.validate(data).observations[1].device_source,'');
});
const invalidRows=[
  ['empty-label',{label:''}],['long-label',{label:'😀'.repeat(257)}],['long-source',{device_source:'😀'.repeat(513)}],
  ['label-type',{label:[]}],['source-type',{device_source:null}],['rssi-string',{rssi:'-60'}],['rssi-bool',{rssi:false}],
  ['rssi-fraction',{rssi:1.5}],['rssi-low',{rssi:-129}],['rssi-high',{rssi:128}],['rssi-infinite',{rssi:Infinity}],
  ['live-index',{reported_index:0}],['list-null',{format:'list'}],['list-negative',{format:'list',reported_index:-1}],
  ['list-high',{format:'list',reported_index:1000000000}],['list-bool',{format:'list',reported_index:true}],
  ['unknown-format',{format:'raw'}],['truncated-bool',{label_truncated:0}],['addressable',{addressable:true}],
  ['zero-id',{observation_id:'0'}],['leading-id',{connection_epoch:'01'}],['numeric-id',{scan_epoch:1}],
  ['oversize-id',{observation_id:'9'.repeat(65)}],['bad-time',{observed_at:'2026-02-30T00:00:00Z'}],
  ['missing-time',{observed_at:undefined}],['oversize-time',{observed_at:'x'.repeat(41)}],
];
for(const [name,extra] of invalidRows)test('invalid '+name+' rejects the whole window atomically',async()=>{
  const f=fixture();let p=f.control.refresh();await flush();f.calls[0].resolve(envelope());assert.equal(await p,true);
  p=f.control.refresh();await flush();f.calls[1].resolve(envelope([row(),row(extra)]));assert.equal(await p,false);
  assert.equal(f.values.length,1);assert.equal(f.states.at(-1),'error');
});
for(const [name,value] of [['null',null],['array',[]],['boolean',{available:1,observations:[]}],
  ['missing',{available:true}],['unavailable-rows',{available:false,observations:[row()]}],
  ['201',envelope(Array(201).fill(row()))],['row-null',envelope([null])],['row-array',envelope([[]])]]) {
  test('invalid envelope '+name,()=>assert.throws(()=>reports.validate(value)));
}
test('list bounds and signed RSSI edges remain admitted',()=>{
  for(const n of [0,999999999])for(const rssi of [-128,127])assert.equal(reports.validate(envelope([row({format:'list',reported_index:n,rssi})])).observations[0].rssi,rssi);
});

function transportModule(fetch, extras={}) {
  const module={exports:{}};
  const context={module,require:()=>targets,fetch,TextDecoder,AbortController,performance,
    window:{CSRF_TOKEN:'fixture-only'},setTimeout,clearTimeout,...extras};
  vm.runInNewContext(source,context);return module.exports;
}
function signalFixture() {
  const ac=new AbortController(),listeners=new Set(),add=ac.signal.addEventListener.bind(ac.signal),remove=ac.signal.removeEventListener.bind(ac.signal);
  ac.signal.addEventListener=(name,fn,opts)=>{if(name==='abort')listeners.add(fn);return add(name,fn,opts);};
  ac.signal.removeEventListener=(name,fn)=>{listeners.delete(fn);return remove(name,fn);};
  return {ac,listeners};
}
test('one deadline covers actual partial body progress and delayed final decoding',async()=>{
  const bytes=new TextEncoder().encode(JSON.stringify(envelope()));let stream;
  const api=transportModule(async()=>new Response(new ReadableStream({start(c){stream=c;c.enqueue(bytes.slice(0,10));}})));
  const f=fixture(reports,{load:api.transport});const p=f.control.refresh();await flush();f.clock(14999);
  assert.equal(f.timers.size,1);assert.equal([...f.timers.values()][0].ms,15000);
  f.clock(15001);stream.enqueue(bytes.slice(10));stream.close();assert.equal(await p,false);
  assert.equal(f.values.length,0);assert.equal(f.timers.size,0);
});
test('real fatal UTF8 stream handles split code points and retains maximum escaped envelope within2MiB',async()=>{
  const data=envelope(Array.from({length:200},(_,i)=>row({label:'😀'.repeat(256),device_source:'😀'.repeat(512),
    observation_id:'9'.repeat(64),connection_epoch:'9'.repeat(64),scan_epoch:'9'.repeat(64),format:'list',reported_index:999999999})));
  const escaped=JSON.stringify(data).replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
  assert(Buffer.byteLength(escaped)<reports.MAX_BYTES);
  const body=new TextEncoder().encode(escaped),s=signalFixture();let offset=0;
  const api=transportModule(async(url,opts)=>{assert.equal(url,'/api/ble-observations');assert.equal(opts.signal,s.ac.signal);
    assert.equal(opts.credentials,'same-origin');assert.equal(opts.redirect,'error');assert.equal(opts.cache,'no-store');
    return new Response(new ReadableStream({pull(c){if(offset===body.length)c.close();else{c.enqueue(body.slice(offset,offset+777));offset=Math.min(body.length,offset+777);}}}));});
  assert.equal(api.validate(await api.transport(s.ac.signal)).observations.length,200);assert.equal(s.listeners.size,0);
  const unicode=new TextEncoder().encode(JSON.stringify(envelope([row({label:'⌚😀'})])));
  const split=transportModule(async()=>new Response(new ReadableStream({start(c){for(const b of unicode)c.enqueue(new Uint8Array([b]));c.close();}})));
  assert.equal((await split.transport(new AbortController().signal)).observations[0].label,'⌚😀');
});
for(const over of [false,true])test('response byte ceiling '+(over?'plus one rejects before JSON.parse':'exactly admitted'),async()=>{
  let parsed=0,cancelled=0;const json={parse:text=>{parsed++;return JSON.parse(text);}};
  const bytes=new TextEncoder().encode('{}'+' '.repeat(reports.MAX_BYTES-2+(over?1:0)));
  const api=transportModule(async()=>({ok:true,body:new ReadableStream({start(c){c.enqueue(bytes);},cancel(){cancelled++;}})}),{JSON:json});
  // Exact-limit body needs its EOF; the oversized body must cancel without another read.
  const exact=over?api:transportModule(async()=>new Response(bytes),{JSON:json});
  if(over){await assert.rejects(exact.transport(new AbortController().signal),/too large/);assert.equal(parsed,0);assert.equal(cancelled,1);}
  else{assert.deepEqual(detached(await exact.transport(new AbortController().signal)),{});assert.equal(parsed,1);}
});
for(const [name,bytes] of [['truncated',new Uint8Array([0x7b,0xe2,0x82])],['invalid',new Uint8Array([0xff])],['json',new TextEncoder().encode('{')]]) {
  test('invalid '+name+' body fails and releases listener',async()=>{const s=signalFixture();const api=transportModule(async()=>new Response(bytes));await assert.rejects(api.transport(s.ac.signal));assert.equal(s.listeners.size,0);});
}
test('missing/unreadable response bodies fail without unbounded fallback',async()=>{
  for(const body of [null,{}, {getReader(){throw Error('reader failed');}}]) {
    const s=signalFixture(),api=transportModule(async()=>({ok:true,body,json(){assert.fail();}}));
    await assert.rejects(api.transport(s.ac.signal));assert.equal(s.listeners.size,0);
  }
});
for(const status of [401,403,503])test('HTTP '+status+' unused body is cancelled without awaiting rejection',async()=>{
  let cancelled=0;const s=signalFixture(),api=transportModule(async()=>({ok:false,status,body:{cancel(){cancelled++;return Promise.reject(Error('cleanup'));}}}));
  await assert.rejects(api.transport(s.ac.signal),e=>e.status===status);await flush();assert.equal(cancelled,1);assert.equal(s.listeners.size,0);
});
test('late headers after abort cancel unused body and do not expose late auth',async()=>{
  let resolve,cancelled=0;const s=signalFixture(),api=transportModule(()=>new Promise(r=>{resolve=r;}));
  const p=api.transport(s.ac.signal);s.ac.abort();resolve({ok:false,status:401,body:{cancel(){cancelled++;return new Promise(()=>{});}}});
  await assert.rejects(p,e=>!e.status);assert.equal(cancelled,1);assert.equal(s.listeners.size,0);
});
test('already aborted request starts no fetch',async()=>{const ac=new AbortController();ac.abort();const api=transportModule(()=>assert.fail());await assert.rejects(api.transport(ac.signal),/retired/);});
test('abort during pending read retries old-engine release failure when read settles',async()=>{
  let settle,pending=true,cancelled=0,released=0,attempts=0;
  const reader={read:()=>new Promise(r=>{settle=()=>{pending=false;r({done:true});};}),
    cancel(){cancelled++;return new Promise(()=>{});},releaseLock(){attempts++;if(pending)throw Error('read pending');released++;}};
  const s=signalFixture(),api=transportModule(async()=>({ok:true,body:{getReader:()=>reader}}));
  const p=api.transport(s.ac.signal);await flush();s.ac.abort();assert.equal(s.listeners.size,0);assert.equal(cancelled,1);assert.equal(released,0);
  settle();await assert.rejects(p,/retired/);assert.equal(released,1);assert(attempts>=2);
});
test('failed read keeps its original error when cancellation and release also fail',async()=>{
  const original=Error('read failed');const s=signalFixture();
  const api=transportModule(async()=>({ok:true,body:{getReader:()=>({read:()=>Promise.reject(original),
    cancel(){throw Error('cancel failed');},releaseLock(){throw Error('release failed');}})}}));
  await assert.rejects(api.transport(s.ac.signal),e=>e===original);assert.equal(s.listeners.size,0);
});
for(const mode of ['throws','rejects','never'])test('reader cleanup '+mode+' cannot strand controller timeout/retry',async()=>{
  let cancelled=0,release=0;
  const api=transportModule(async()=>({ok:true,body:{getReader:()=>({read:()=>new Promise(()=>{}),
    cancel(){cancelled++;if(mode==='throws')throw Error('cleanup');return mode==='rejects'?Promise.reject(Error('cleanup')):new Promise(()=>{});},releaseLock(){release++;}})}}));
  const actual=fixture(reports,{load:api.transport});const p=actual.control.refresh();await flush();actual.expire();
  assert.equal(await p,false);assert.equal(actual.timers.size,0);assert.equal(cancelled,1);assert.equal(release,1);
  const next=actual.control.refresh();await flush();actual.control.suspend();assert.equal(await next,false);assert.equal(actual.timers.size,0);await flush();
});

// Cancellation is a synchronous callback boundary, before the replacing caller owns a new slot.
function cancellationReentry(kind, mode) {
  let f, inner, once=true;
  const pending=[];
  function reenter() {
    if(!once)return;once=false;
    if(mode==='replace')inner=f.control.refresh(true);
    else if(mode==='direct-resume')inner=f.control.resume();
    else {f.control.suspend();if(mode==='resume')inner=f.control.resume();}
  }
  const load=kind==='listener'?signal=>new Promise((resolve,reject)=>{
    pending.push({resolve,reject});if(pending.length===1)signal.addEventListener('abort',reenter,{once:true});
  }):transportModule(async()=>({ok:true,body:new ReadableStream({
    start(controller){pending.push({resolve:value=>{controller.enqueue(new TextEncoder().encode(JSON.stringify(value)));controller.close();},reject:error=>controller.error(error)});},
    cancel:reenter,
  })})).transport;
  f=fixture(reports,{load});return {f,pending,get inner(){return inner;}};
}
for(const kind of ['listener','stream'])for(const mode of ['replace','resume'])for(const outcome of ['success','failure','auth','timeout']) {
  test(kind+' cancellation '+mode+' preserves newest '+outcome+' and retry ownership',async()=>{
    const h=cancellationReentry(kind,mode),f=h.f;
    const old=f.control.refresh();await flush();const outer=f.control.refresh(true);
    let outerSettled=false,outerValue;outer.then(value=>{outerSettled=true;outerValue=value;});
    try {
      await flush();assert.equal(await old,false);assert.equal(h.pending.length,2);assert.equal(f.timers.size,1);
      if(outcome==='success')h.pending[1].resolve(envelope());
      else if(outcome==='failure')h.pending[1].reject(Error('inert failure'));
      else if(outcome==='auth')h.pending[1].reject({status:403});
      else f.expire();
      assert.equal(await h.inner,outcome==='success');await flush();
      assert.equal(outerSettled,true,'Superseded outer refresh must settle without overwriting the inner owner');
      assert.equal(outerValue,false);assert.equal(f.timers.size,0);
      assert.equal(f.states.at(-1),outcome==='success'?'fresh':outcome==='auth'?'unauthorized':'error');
      const retry=f.control.refresh();assert.notEqual(retry,outer);await flush();assert.equal(h.pending.length,3);
      h.pending[2].resolve(envelope());assert.equal(await retry,true);assert.equal(f.timers.size,0);
    } finally {f.control.suspend();await outer;await flush();}
  });
}
for(const kind of ['listener','stream'])test(kind+' cancellation suspension installs no replacement slot',async()=>{
  const h=cancellationReentry(kind,'suspend'),f=h.f;
  const old=f.control.refresh();await flush();const outer=f.control.refresh(true);let settled=false;
  outer.then(()=>{settled=true;});
  try {
    await flush();assert.equal(await old,false);assert(settled);assert.equal(await outer,false);
    assert.equal(h.pending.length,1);assert.equal(f.timers.size,0);assert.equal(await f.control.refresh(),false);
    const retry=f.control.resume();await flush();h.pending[1].resolve(envelope());assert.equal(await retry,true);assert.equal(f.timers.size,0);
  } finally {f.control.suspend();await outer;await flush();}
});
for(const kind of ['listener','stream'])for(const outcome of ['success','failure','auth','timeout']) {
  test(kind+' direct suspension callback resume owns newest '+outcome,async()=>{
    const h=cancellationReentry(kind,'direct-resume'),f=h.f;
    const old=f.control.refresh();await flush();f.control.suspend();
    try {
      await flush();assert.equal(await old,false);assert.notEqual(h.inner,old);
      assert.equal(h.pending.length,2);assert.equal(f.control.resume(),h.inner);assert.equal(f.timers.size,1);
      if(outcome==='success')h.pending[1].resolve(envelope());
      else if(outcome==='failure')h.pending[1].reject(Error('inert failure'));
      else if(outcome==='auth')h.pending[1].reject({status:403});
      else f.expire();
      assert.equal(await h.inner,outcome==='success');assert.equal(f.timers.size,0);
      const retry=f.control.refresh();await flush();assert.equal(h.pending.length,3);
      h.pending[2].resolve(envelope());assert.equal(await retry,true);assert.equal(f.states.at(-1),'fresh');assert.equal(f.timers.size,0);
    } finally {f.control.suspend();await flush();}
  });
}
