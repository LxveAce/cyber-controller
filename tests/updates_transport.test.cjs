const test = require('node:test');
const assert = require('node:assert/strict');
const transport = require('../src/ui/web/static/updates_transport.js');
const card = require('../src/ui/web/static/updates_card.js');
const runtime = 'a'.repeat(32), operation = 'b'.repeat(32);
const current = {ok:true,status:'UP_TO_DATE',current:'2.0.1',behind:0,latest_tag:'v2.0.1',latest_url:''};
const newer = {ok:true,status:'NEWER',current:'2.0.1',behind:3,latest_tag:'v2.0.4',latest_url:card.RELEASES+'/tag/v2.0.4'};
const view = (phase, result=null, extra={}) => ({schema_version:2,runtime_id:runtime,operation_id:operation,
  phase,result,retirement_reason:null,...extra});
const admission = (phase='queued', result=null, extra={}) => view(phase,result,{accepted:true,reason:'started',...extra});
const flush = async () => { for(let i=0;i<12;i++) await Promise.resolve(); };
function deferred() { let resolve,reject; const promise=new Promise((a,b)=>{resolve=a;reject=b;}); return {promise,resolve,reject}; }
function fixture() {
  const calls=[],timers=new Map(); let sequence=0;
  const client=transport.create({fetch:(url,options)=>{const d=deferred();calls.push({url,options,...d});return d.promise;},
    csrfToken:()=> 'fixture-csrf',setTimeout:(fn,ms)=>{const id=++sequence;timers.set(id,{fn,ms});return id;},
    clearTimeout:id=>timers.delete(id)});
  const control=new AbortController();
  function reply(index,value,status=index===0?202:200) {calls[index].resolve({ok:status>=200&&status<300,status,json:()=>Promise.resolve(value)});}
  function tick() {assert.equal(timers.size,1); const [id,t]=timers.entries().next().value;timers.delete(id);assert.equal(t.ms,1000);t.fn();}
  return {client,calls,timers,control,reply,tick};
}

test('construction is inert and a manual admission uses authenticated same-origin schema2',async()=>{
  const f=fixture(); assert.equal(f.calls.length,0);const promise=f.client.check(f.control.signal);await flush();
  assert.equal(f.calls.length,1);const c=f.calls[0];assert.equal(c.url,'/api/updates/check');
  assert.equal(c.options.method,'POST');assert.equal(c.options.credentials,'same-origin');
  assert.equal(c.options.cache,'no-store');assert.equal(c.options.redirect,'error');
  assert.equal(c.options.headers['X-CSRF-Token'],'fixture-csrf');
  assert.deepEqual(JSON.parse(c.options.body),{schema_version:2});
  f.reply(0,admission('completed',current));assert.deepEqual(await promise,current);assert.equal(f.timers.size,0);
});

test('polls serially for one exact operation and returns the exact newer count',async()=>{
  const f=fixture();let settled=false;const promise=f.client.check(f.control.signal).then(x=>{settled=true;return x;});await flush();
  f.reply(0,admission('queued'));await flush();assert.equal(settled,false);assert.equal(f.calls.length,1);
  f.tick();await flush();assert.equal(f.calls.length,2);assert.equal(f.timers.size,0);
  assert.equal(f.calls[1].url,`/api/updates/status?runtime_id=${runtime}&operation_id=${operation}`);
  assert.equal(f.calls[1].options.method,'GET');assert.equal(f.calls[1].options.body,undefined);
  f.reply(1,view('checking'));await flush();f.tick();await flush();assert.equal(f.calls.length,3);
  f.reply(2,view('completed',newer));assert.equal((await promise).behind,3);assert.equal(f.timers.size,0);
});

test('coalesced fast completion and clean offline remain valid',async()=>{
  for(const terminal of [current,{ok:false,status:'OFFLINE',current:'2.0.1'}]) {
    const f=fixture(),p=f.client.check(f.control.signal);await flush();
    f.reply(0,admission('completed',terminal,{reason:'coalesced'}));assert.deepEqual(await p,terminal);
  }
});

test('pre-aborted and missing signals never dispatch',async()=>{
  const f=fixture();f.control.abort();await assert.rejects(f.client.check(f.control.signal));
  await assert.rejects(f.client.check(null));assert.equal(f.calls.length,0);assert.equal(f.timers.size,0);
});

test('abort cancels pending poll and late ignored-abort response cannot poll',async()=>{
  for(const when of ['waiting','fetch','body']) {
    const f=fixture(),p=f.client.check(f.control.signal);const rejected=assert.rejects(p);await flush();
    if(when==='waiting') {f.reply(0,admission());await flush();assert.equal(f.timers.size,1);}
    let body;
    if(when==='body') {body=deferred();f.calls[0].resolve({ok:true,status:202,json:()=>body.promise});await flush();}
    f.control.abort();await rejected;assert.equal(f.timers.size,0);
    if(body)body.resolve(admission());else if(when==='fetch')f.reply(0,admission());
    await flush();assert.equal(f.calls.length,1);assert.equal(f.timers.size,0);
  }
});

test('network errors and non-contract HTTP statuses reject without polling',async()=>{
  for(const status of [200,204,302,400,409,410,429,500,503]) {
    const f=fixture(),p=f.client.check(f.control.signal);const rejected=assert.rejects(p);await flush();
    f.reply(0,admission('completed',current),status);await rejected;assert.equal(f.timers.size,0);
  }
  const f=fixture(),p=f.client.check(f.control.signal);const rejected=assert.rejects(p);await flush();
  f.calls[0].reject(Error('fixture unavailable'));await rejected;assert.equal(f.timers.size,0);
});

test('malformed admissions never substitute terminal state for acceptance',async()=>{
  for(const bad of [null,[],current,admission('completed',current,{accepted:false}),
    admission('completed',current,{schema_version:1}),admission('completed',current,{reason:'cooldown'}),
    admission('completed',current,{operation_id:operation+'\n'}),admission('completed',current,{runtime_id:'x'}),
    admission('queued',current),admission('unknown'),admission('completed',null)]) {
    const f=fixture(),p=f.client.check(f.control.signal);const rejected=assert.rejects(p);await flush();
    f.reply(0,bad);await rejected;assert.equal(f.timers.size,0);
  }
});

test('poll identity changes, phase reversal and explicit failure are terminal errors',async()=>{
  for(const bad of [view('completed',newer,{runtime_id:'c'.repeat(32)}),
    view('completed',newer,{operation_id:'c'.repeat(32)}),view('queued'),
    view('retired',null,{error:'retired',retirement_reason:'closed'}),
    view('completed',null,{error:'classification_error'}),view('checking',current)]) {
    const f=fixture(),p=f.client.check(f.control.signal);const rejected=assert.rejects(p);await flush();
    f.reply(0,admission('checking'));await flush();f.tick();await flush();f.reply(1,bad);
    await rejected;assert.equal(f.calls.length,2);assert.equal(f.timers.size,0);
  }
});

test('completed results use unchanged strict controller validation',async()=>{
  for(const terminal of [{...current,behind:1},{...newer,behind:0},
    {ok:false,status:'OFFLINE',current:'2.0.1',latest_tag:'old'},
    {ok:true,status:'OFFLINE',current:'2.0.1'},{ok:false,status:'OFFLINE'},
    {...current,current:'2.0.1\n'}]) {
    const f=fixture(),p=f.client.check(f.control.signal);const rejected=assert.rejects(p);await flush();
    f.reply(0,admission('completed',terminal));await rejected;assert.equal(f.timers.size,0);
  }
});

test('card total timeout owns transport and pageshow does not start another check',async()=>{
  const f=fixture(),states=[],deadlines=new Map();let now=0,id=0;
  const owner=card.create({getVersion:()=>({version:'2.0.1'}),check:f.client.check,onState:x=>states.push(x),
    now:()=>now,setTimeout:fn=>{deadlines.set(++id,fn);return id;},clearTimeout:key=>deadlines.delete(key)});
  const first=owner.check();await flush();assert.equal(owner.check(),first);assert.equal(f.calls.length,1);
  f.reply(0,admission());await flush();f.tick();await flush();assert.equal(f.calls.length,2);
  now=30000;for(const fn of [...deadlines.values()])fn();assert.equal(await first,false);
  assert.equal(f.timers.size,0);assert.equal(states.at(-1).busy,false);
  assert(f.calls[1].options.signal.aborted);f.reply(1,view('completed',newer));await flush();
  assert.notEqual(states.at(-1).kind,'newer');owner.suspend();owner.resume();await flush();assert.equal(f.calls.length,2);
});

test('card pagehide retires a body read and a fresh manual retry gets a new identity',async()=>{
  const f=fixture(),states=[];
  const owner=card.create({getVersion:()=>({version:'2.0.1'}),check:f.client.check,onState:x=>states.push(x)});
  const first=owner.check();await flush();const body=deferred();
  f.calls[0].resolve({ok:true,status:202,json:()=>body.promise});await flush();owner.suspend();
  assert.equal(await first,false);owner.resume();await flush();assert.equal(f.calls.length,1);
  const retry=owner.check();await flush();assert.equal(f.calls.length,2);
  f.reply(1,admission('completed',newer,{operation_id:'c'.repeat(32)}),202);assert.equal(await retry,true);
  body.resolve(admission('completed',current));await flush();assert.equal(states.at(-1).latestTag,'v2.0.4');
  owner.suspend();assert.equal(f.timers.size,0);
});
