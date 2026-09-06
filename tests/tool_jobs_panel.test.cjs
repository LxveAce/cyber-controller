const test = require('node:test');
const assert = require('node:assert/strict');
const panel = require('../src/ui/web/static/tool_jobs_panel.js');
const A = 'a'.repeat(32), B = 'b'.repeat(32);
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b; }); return {promise,resolve,reject}; }
function memory(initial=null) {
  let value=initial;
  return { getItem:()=>value, setItem:(key,next)=>{value=next;}, removeItem:()=>{value=null;} };
}
function snapshot(id=A, state='running', extra={}) {
  return {status:200,body:{job_id:id,tool:'aircrack',state,phase:'extracting',completed:1,total:5,
    error:'',log:['one line'],active:!['succeeded','failed','cancelled'].includes(state),...extra}};
}
function setup(t, request, storage=memory(), extra={}) {
  const calls=[];
  const api=panel.create({storage,request:async(...args)=>{calls.push(args.slice(0,3));return request(...args);},...extra});
  t.after(()=>api.dispose());
  return {api,calls,storage};
}
const tracked = (id=A, more={}) => JSON.stringify({version:1,kind:'bundled',name:'pack',jobId:id,cancelSent:false,...more});

for (const downloadFirst of [false, true]) {
  test(`bundled and download share their pre-dispatch reservation: download first ${downloadFirst}`, async t => {
    const reply=deferred();
    const {api,calls,storage}=setup(t,method=>method==='POST'?reply.promise:snapshot());
    const first=downloadFirst ? api.startDownload('aircrack') : api.start('pack');
    assert.equal(await (downloadFirst ? api.start('pack') : api.startDownload('aircrack')),false);
    await tick();
    assert.equal(calls.length,1);
    assert.equal(calls[0][1],downloadFirst?'/api/crack/install-tool/async':'/api/crack/enable-bundled/async');
    assert.deepEqual(calls[0][2],downloadFirst?{tool:'aircrack'}:{pack:'pack'});
    assert.equal(JSON.parse(storage.getItem()).kind,downloadFirst?'download':'bundled');
    reply.resolve({status:202,body:{job_id:A}});
    await first;
  });
}

test('valid download rejection releases the shared lock without a fallback POST',async t=>{
  const {api,calls}=setup(t,()=>({status:422,body:{error:'unsupported archive'}}));
  await api.startDownload('aircrack');
  assert.equal(api.getState().mode,'rejected');
  assert.equal(api.getState().locked,false);
  assert.deepEqual(calls.map(c=>c[1]),['/api/crack/install-tool/async']);
});

test('unreadable download rejection retains ownership without retry',async t=>{
  const {api,calls}=setup(t,()=>({status:503,body:null}));
  await api.startDownload('aircrack');
  assert.equal(api.getState().mode,'unresolved');
  assert.equal(await api.start('pack'),false);
  assert.equal(calls.length,1);
});

test('restored download retains method metadata and only requests GETs',async t=>{
  const result={status:200,body:{schema_version:1,tool:'aircrack',path:'inert/tool.exe',version:'1',
    source:'download',verification_method:'size',state:'succeeded'}};
  const {api,calls}=setup(t,(_method,path)=>path.endsWith('/result')?result:snapshot(A,'succeeded'),
    memory(tracked(A,{kind:'download',name:'aircrack'})));
  await tick();
  assert.equal(api.getState().job.result.verification_method,'size');
  assert.equal(api.getState().job.resultStatus,'available');
  assert.equal(calls.every(c=>c[0]==='GET'),true);
});

test('restored download cancel marker suppresses another request',async t=>{
  const {api,calls}=setup(t,()=>snapshot(),memory(tracked(A,{kind:'download',cancelSent:true})));
  await tick();
  assert.equal(api.canCancel(),false);
  assert.equal(await api.cancel(),false);
  assert.equal(calls.every(c=>c[0]==='GET'),true);
});

test('a pending download marker never repeats its lost start',async t=>{
  const {api,calls}=setup(t,()=>{throw Error('must not request');},memory(tracked(null,{kind:'download'})));
  assert.equal(api.getState().mode,'unresolved');
  assert.equal(await api.startDownload('aircrack'),false);
  assert.equal(calls.length,0);
});

test('download tracking failure before dispatch sends nothing',async t=>{
  const storage={...memory(),setItem(){throw Error('quota');}};
  const {api,calls}=setup(t,()=>snapshot(),storage);
  assert.equal(await api.startDownload('aircrack'),false);
  assert.equal(calls.length,0);
});

test('different-pack double click owns one POST and the first marker', async t=>{
  const reply=deferred();
  const {api,calls,storage}=setup(t,(method)=>method==='POST'?reply.promise:snapshot());
  const first=api.start('first');
  assert.equal(await api.start('second'),false);
  await tick(); assert.equal(calls.length,1); assert.equal(JSON.parse(storage.getItem()).name,'first');
  reply.resolve({status:202,body:{job_id:A}}); await first;
  assert.equal(api.getState().job.jobId,A); assert.equal(JSON.parse(storage.getItem()).jobId,A);
});
test('refresh only observes and never repeats start', async t=>{
  const {api,calls}=setup(t,(method)=>method==='POST'?{status:202,body:{job_id:A}}:snapshot());
  await api.start('pack'); await api.refresh(); await api.refresh();
  assert.equal(calls.filter(c=>c[0]==='POST').length,1);
});
test('reload with ID only watches; cancellation marker suppresses another cancel', async t=>{
  const {api,calls}=setup(t,()=>snapshot(),memory(tracked(A,{cancelSent:true})));
  await tick(); assert.equal(calls.length,1); assert.equal(calls[0][0],'GET');
  assert.equal(api.canCancel(),false); assert.equal(await api.cancel(),false);
});
for(const [name,raw] of [['pending',tracked(null)],['legacy',tracked(null,{kind:'legacy'})],
  ['invalid JSON','{'],['oversized','x'.repeat(1025)],['invalid ID',tracked('bad')]]) {
  test('reload '+name+' remains unresolved without POST',async t=>{
    const {api,calls}=setup(t,()=>{throw Error('must not request');},memory(raw));
    assert.equal(api.getState().mode,'unresolved'); assert.equal(await api.start('new'),false);
    assert.equal(calls.length,0);
  });
}
test('storage read failure stays unresolved', async t=>{
  const storage={...memory(),getItem(){throw Error('denied');}};
  const {api,calls}=setup(t,()=>snapshot(),storage);
  assert.equal(api.getState().locked,true); assert.equal(calls.length,0);
});
test('initial storage write failure sends nothing', async t=>{
  const storage={...memory(),setItem(){throw Error('quota');}};
  const {api,calls}=setup(t,()=>snapshot(),storage);
  assert.equal(await api.start('pack'),false); assert.equal(calls.length,0);
  assert.match(api.getState().message,/not started/);
});
test('ID persistence failure keeps live observation without another POST', async t=>{
  const storage=memory(); let writes=0; const set=storage.setItem;
  storage.setItem=(...args)=>{if(++writes>1)throw Error('quota');set(...args);};
  const {api,calls}=setup(t,method=>method==='POST'?{status:202,body:{job_id:A}}:snapshot(),storage);
  await api.start('pack');
  assert.equal(api.getState().job.jobId,A); assert.equal(JSON.parse(storage.getItem()).jobId,null);
  assert.match(api.getState().storageNote,/could not be saved/);
  assert.equal(calls.filter(c=>c[0]==='POST').length,1);
});
for(const response of [{status:503,body:null},{status:202,body:{}},{status:500,body:{error:'failed'}},
  {status:503,body:[]},{status:403,body:{error:''}}]) {
  test('unreadable/uncertain POST '+JSON.stringify(response)+' retains lock', async t=>{
    const {api,storage}=setup(t,()=>response);
    await api.start('pack'); assert.equal(api.getState().mode,'unresolved');
    assert.equal(api.getState().locked,true); assert.ok(storage.getItem());
    assert.equal(await api.start('again'),false);
  });
}
test('definitive JSON rejection unlocks without pretending to install', async t=>{
  const {api,storage}=setup(t,()=>({status:409,body:{error:'busy'}}));
  await api.start('pack'); assert.equal(api.getState().mode,'rejected');
  assert.equal(api.getState().locked,false); assert.equal(storage.getItem(),null);
});
test('terminal success survives missing result metadata', async t=>{
  const {api,storage}=setup(t,(method,path)=>method==='POST'?{status:202,body:{job_id:A}}:
    path.endsWith('/result')?{status:503,body:{error:'offline'}}:snapshot(A,'succeeded'));
  await api.start('pack'); const state=api.getState();
  assert.equal(state.mode,'terminal'); assert.equal(state.job.snapshot.state,'succeeded');
  assert.equal(state.job.resultStatus,'unavailable'); assert.equal(state.locked,false); assert.equal(storage.getItem(),null);
});
for(const status of [401,404]) {
  test('status '+status+' keeps unresolved ownership', async t=>{
    const {api}=setup(t,method=>method==='POST'?{status:202,body:{job_id:A}}:{status,body:{error:'unavailable'}});
    await api.start('pack'); assert.equal(api.getState().locked,true);
    assert.equal(api.getState().job.observation,'paused');
  });
}
test('forget during initial POST retires its later ID', async t=>{
  const old=deferred(); let starts=0;
  const {api,storage}=setup(t,(method)=>method==='POST'?(++starts===1?old.promise:{status:202,body:{job_id:B}}):snapshot(B));
  const first=api.start('old'); await tick(); api.forget();
  await api.start('new'); old.resolve({status:202,body:{job_id:A}}); await first; await tick();
  assert.equal(JSON.parse(storage.getItem()).jobId,B); assert.equal(api.getState().name,'new');
});
test('forget after cancel request ignores late cancel response', async t=>{
  const cancel=deferred();
  const {api,storage}=setup(t,(method,path)=>path.endsWith('/cancel')?cancel.promise:
    method==='POST'?{status:202,body:{job_id:A}}:snapshot());
  await api.start('pack'); const pending=api.cancel(); await tick();
  assert.equal(JSON.parse(storage.getItem()).cancelSent,true);
  api.forget(); cancel.resolve({status:200,body:{cancel_requested:true}}); await pending;
  assert.equal(api.getState().mode,'idle'); assert.equal(storage.getItem(),null);
});
test('failed removal is not presented as forgotten', async t=>{
  const storage=memory(tracked(null)); storage.removeItem=()=>{throw Error('denied');};
  const {api}=setup(t,()=>snapshot(),storage);
  assert.equal(api.forget(),false); assert.equal(api.getState().locked,true);
  assert.equal(await api.start('new'),false);
});
test('cancel can be retried after a recovered pre-dispatch storage failure', async t=>{
  const storage=memory(); const save=storage.setItem; let fail=false, cancels=0;
  storage.setItem=(...args)=>{if(fail)throw Error('quota');save(...args);};
  const {api}=setup(t,(method,path)=>path.endsWith('/cancel')?(cancels++,{status:200,body:{cancel_requested:true}}):
    method==='POST'?{status:202,body:{job_id:A}}:snapshot(),storage);
  await api.start('pack');fail=true;
  assert.equal(await api.cancel(),false);assert.equal(cancels,0);assert.equal(api.canCancel(),true);
  assert.equal(api.getState().cancellationRecorded,false);assert.match(api.getState().storageNote,/not sent/);
  fail=false;await api.cancel();assert.equal(cancels,1);assert.equal(api.canCancel(),false);
  assert.equal(api.getState().storageNote,'');assert.equal(api.getState().job.cancel,'requested');
  assert.equal(await api.cancel(),false);assert.equal(cancels,1);
});
test('terminal tracking removal clears a now-obsolete persistence warning', async t=>{
  const storage=memory();let writes=0;const save=storage.setItem;
  storage.setItem=(...args)=>{if(++writes>1)throw Error('quota');save(...args);};
  const {api}=setup(t,(method,path)=>method==='POST'?{status:202,body:{job_id:A}}:
    path.endsWith('/result')?{status:404,body:{error:'gone'}}:snapshot(A,'succeeded'),storage);
  await api.start('pack');assert.equal(storage.getItem(),null);assert.equal(api.getState().storageNote,'');
});
test('async and legacy actions share the same lock in both directions', async t=>{
  const result=deferred();
  const {api,calls}=setup(t,method=>method==='POST'?{status:202,body:{job_id:A}}:snapshot());
  const legacy=api.runLegacy('download',()=>result.promise);
  assert.equal(await api.start('pack'),false); assert.equal(calls.length,0);
  result.resolve(); await legacy; await api.start('pack');
  let ran=false; assert.equal(await api.runLegacy('download',()=>{ran=true;}),false); assert.equal(ran,false);
});
test('legacy failure leaves an unknown request, never a success', async t=>{
  const {api,storage}=setup(t,()=>snapshot());
  await api.runLegacy('download',()=>Promise.reject(Error('lost')));
  assert.equal(api.getState().mode,'unresolved'); assert.ok(storage.getItem());
});
test('transport retains status on invalid, oversized or interrupted JSON bodies', async t=>{
  const oldFetch=global.fetch, oldWindow=global.window;
  t.after(()=>{global.fetch=oldFetch;global.window=oldWindow;});
  global.window={CSRF_TOKEN:'fixture-only'};
  let sent;
  for(const body of ['{', ' '.repeat(1024*1024+1),new ReadableStream({start(c){c.error(Error('dropped'));}})]) {
    global.fetch=async(path,options)=>{sent=options;return new Response(body,{status:503});};
    const result=await panel.transport('POST','/api/crack/enable-bundled/async',{pack:'p'});
    assert.deepEqual(result,{status:503,body:null}); assert.equal(sent.redirect,'error');
    assert.equal(sent.credentials,'same-origin'); assert.equal(sent.headers['X-CSRF-Token'],'fixture-only');
  }
});
