const test = require('node:test'), assert = require('node:assert/strict');
const card = require('../src/ui/web/static/updates_card.js');
const current = {ok:true,status:'UP_TO_DATE',current:'2.0.1',latest_tag:'v2.0.1',latest_url:'',behind:0};
const newer = {ok:true,status:'NEWER',current:'2.0.1',latest_tag:'v9.9.9',latest_url:card.RELEASES+'/tag/v9.9.9',behind:1};
const flush = async () => { for (let i=0;i<12;i++) await Promise.resolve(); };
function fixture(overrides={}) {
  const versions=[],checks=[],states=[],timers=new Map();let next=0,clock=0;
  const options={getVersion:signal=>new Promise((resolve,reject)=>versions.push({signal,resolve,reject})),
    check:signal=>new Promise((resolve,reject)=>checks.push({signal,resolve,reject})),
    onState:value=>states.push(value),now:()=>clock,
    setTimeout:(fn,ms)=>{timers.set(++next,{fn,ms});return next;},clearTimeout:id=>timers.delete(id),...overrides};
  const control=card.create(options);
  return {control,versions,checks,states,timers,options,setClock:v=>{clock=v;},
    expire:()=>{clock+=30000;for(const timer of [...timers.values()])timer.fn();}};
}

test('only explicit successful terminal replies can claim up to date',()=>{
  assert.equal(card.parseReply(current).kind,'up_to_date');
  for(const reply of [null,{},[],{...current,ok:false},{...current,ok:1},{...current,status:'QUEUED'},
    {...current,current:undefined},{...current,current:0},{...current,behind:-1},{...current,behind:0.5},
    {...current,latest_tag:undefined},{...current,current:'x'.repeat(121)},{...current,current:'2.0.1\n'}]) {
    assert.throws(()=>card.parseReply(reply,'2.0.1'));
  }
});
test('offline accepts actual producer shapes, but rejects present malformed fields',()=>{
  for(const ok of [true,false]) {
    assert.deepEqual(card.parseReply({ok,status:'OFFLINE'},'2.0.1'),{kind:'offline',current:'2.0.1'});
    assert.equal(card.parseReply({ok,status:'OFFLINE'}).current,null);
  }
  for(const extra of [{ok:'false'},{current:null},{current:''},{current:undefined},{behind:1},{latest_tag:'v9'},
    {latest_url:'https://other.invalid'}])assert.throws(()=>card.parseReply({ok:false,status:'OFFLINE',...extra},'2.0.1'));
});
test('newer fields are validated and only its URL has a safe fallback',()=>{
  assert.equal(card.parseReply(newer).latestURL,newer.latest_url);
  assert.equal(card.parseReply({...newer,latest_url:'https://other.invalid/'}).latestURL,card.RELEASES);
  for(const extra of [{ok:false},{current:null},{latest_tag:''},{latest_tag:'v9.9.9\r\n'},{latest_tag:'<script>'},{behind:0}]) {
    assert.throws(()=>card.parseReply({...newer,...extra}));
  }
});
test('canonical release URLs reject ambiguous browser-normalized destinations',()=>{
  for(const url of [card.RELEASES,card.RELEASES+'/tag/v2.0.1-beta+build']) assert.equal(card.releaseURL(url),url);
  for(const url of [null,5,'',card.RELEASES+'/',card.RELEASES+'/tag/..',card.RELEASES+'/tag/%2e%2e/elsewhere',
    card.RELEASES+'/tag/a%2fb',card.RELEASES+'/tag/a\\b',card.RELEASES+'?x=1',card.RELEASES+'#tag',
    'https://github.com:443/LxveAce/cyber-controller/releases','https://user@github.com/LxveAce/cyber-controller/releases',
    'https://github.com.evil.invalid/LxveAce/cyber-controller/releases',card.RELEASES.replace('https:','http:'),
    '\n'+card.RELEASES,card.RELEASES+'/tag/v9.9.9\n',card.RELEASES+'/tag/'+ 'x'.repeat(121)]) assert.equal(card.releaseURL(url),card.RELEASES);
});

for(const failure of [false,true])test('late initial version '+(failure?'error':'success')+' cannot overwrite manual completion',async()=>{
  const f=fixture(),version=f.control.start(),p=f.control.check();await flush();
  f.checks[0].resolve(newer);assert.equal(await p,true);const before=f.states.at(-1);
  failure?f.versions[0].reject(Error('fixture')):f.versions[0].resolve({version:'2.0.1'});await version;
  assert.equal(f.states.at(-1),before);assert(Object.isFrozen(before));assert.equal(f.timers.size,0);
});
test('single-flight starts before transport and re-enables after completion',async()=>{
  const f=fixture(),p=f.control.check();assert.equal(f.control.check(),p);assert.equal(f.states.at(-1).busy,true);
  await flush();assert.equal(f.checks.length,1);f.checks[0].resolve(current);assert.equal(await p,true);
  assert.equal(f.states.at(-1).busy,false);const next=f.control.check();await flush();assert.equal(f.checks.length,2);
  f.checks[1].resolve(newer);assert.equal(await next,true);assert.equal(f.timers.size,0);
});
test('synchronous transport throw settles and allows retry',async()=>{
  let calls=0;const f=fixture({check:()=>{if(++calls===1)throw Error('fixture');return current;}});
  assert.equal(await f.control.check(),false);assert.equal(f.states.at(-1).kind,'error');
  assert.equal(await f.control.check(),true);assert.equal(f.timers.size,0);
});
for(const kind of ['version','check'])test(kind+' settling exactly at delayed deadline is expired',async()=>{
  const f=fixture(),p=kind==='version'?f.control.start():f.control.check();await flush();f.setClock(30000);
  (kind==='version'?f.versions:f.checks)[0].resolve(kind==='version'?{version:'2.0.1'}:newer);
  assert.equal(await p,false);assert.equal(f.timers.size,0);
  assert.equal(f.states.at(-1).kind,kind==='version'?'version_unavailable':'error');
});
test('expired before transport dispatch performs no request',async()=>{
  const f=fixture(),p=f.control.check();f.setClock(30000);assert.equal(await p,false);assert.equal(f.checks.length,0);
});
test('timeout permits retry and ignored-abort old success cannot win',async()=>{
  const f=fixture(),p=f.control.check();await flush();f.expire();assert.equal(await p,false);assert(f.checks[0].signal.aborted);
  const next=f.control.check();await flush();f.checks[1].resolve(newer);assert.equal(await next,true);
  const before=f.states.at(-1);f.checks[0].resolve(current);await flush();assert.equal(f.states.at(-1),before);
});
test('abort reentry cannot re-enable or overwrite a new retry',async()=>{
  const f=fixture();let next;const p=f.control.check();await flush();
  f.checks[0].signal.addEventListener('abort',()=>{next=f.control.check();});f.expire();
  assert.equal(await p,false);await flush();assert.equal(f.states.at(-1).kind,'checking');assert.equal(f.states.at(-1).busy,true);
  f.checks[1].resolve(newer);assert.equal(await next,true);assert.equal(f.timers.size,0);
});
test('timer cleanup reentry cannot clear a new operation',async()=>{
  let f,next,reentered=false;
  f=fixture({clearTimeout:id=>{f.timers.delete(id);if(!reentered){reentered=true;next=f.control.check();}}});
  const p=f.control.check();await flush();f.checks[0].resolve(current);assert.equal(await p,false);await flush();
  assert.equal(f.states.at(-1).kind,'checking');assert.equal(f.control.check(),next);
  f.checks[1].resolve(newer);assert.equal(await next,true);assert.equal(f.timers.size,0);
});
test('loading callback can suspend before any transport or timer is allocated',async()=>{
  let f;f=fixture({onState:state=>{if(state.kind==='checking')f.control.suspend();}});
  assert.equal(await f.control.check(),false);await flush();assert.equal(f.checks.length,0);assert.equal(f.timers.size,0);
});
test('pagehide retires both requests and pageshow does not start network work',async()=>{
  const f=fixture(),v=f.control.start(),p=f.control.check();await flush();f.control.suspend();f.control.resume();f.control.resume();
  assert.equal(await v,false);assert.equal(await p,false);assert.equal(f.states.at(-1).kind,'cancelled');
  assert.equal(f.states.at(-1).busy,false);assert(f.versions[0].signal.aborted);assert(f.checks[0].signal.aborted);
  f.versions[0].resolve({version:'2.0.1'});f.checks[0].resolve(newer);await flush();assert.equal(f.states.at(-1).kind,'cancelled');
  assert.equal(f.versions.length,1);assert.equal(f.checks.length,1);assert.equal(f.timers.size,0);
});
test('pagehide abort listener may resume and start a surviving retry',async()=>{
  const f=fixture(),v=f.control.start(),p=f.control.check();let next;await flush();
  f.versions[0].signal.addEventListener('abort',()=>{f.control.resume();next=f.control.check();});
  f.control.suspend();await flush();assert.equal(await v,false);assert.equal(await p,false);
  assert.equal(f.states.at(-1).kind,'checking');assert.equal(f.checks[1].signal.aborted,false);
  f.checks[1].resolve(newer);assert.equal(await next,true);
});
test('completed state and cached version survive hide/resume',async()=>{
  const f=fixture(),p=f.control.check();await flush();f.checks[0].resolve(current);await p;
  f.control.suspend();f.control.resume();assert.equal(f.states.at(-1).kind,'up_to_date');
  const next=f.control.check();await flush();f.checks[1].resolve({ok:false,status:'OFFLINE'});await next;
  assert.equal(f.states.at(-1).current,'2.0.1');assert.equal(f.states.at(-1).kind,'offline');
});
test('throwing render observer cannot strand a current request',async()=>{
  const f=fixture({onState:()=>{throw Error('fixture renderer');}}),p=f.control.check();await flush();
  f.checks[0].resolve(current);assert.equal(await p,true);assert.equal(f.timers.size,0);
  const next=f.control.check();await flush();f.checks[1].resolve(newer);assert.equal(await next,true);
});
