const test=require('node:test'),assert=require('node:assert/strict');
const selection=require('../src/ui/web/static/target_selection.js');
const snapshots=require('../src/ui/web/static/target_snapshots.js');
const stamp='2026-09-06T12:00:00.123456+00:00';
const target=(n,extra={})=>({target_type:'ap',mac:'02:00:00:00:'+Math.floor(n/256).toString(16).padStart(2,'0')+':'+(n%256).toString(16).padStart(2,'0'),ssid:'Same name',timestamp:stamp,device_source:'A',...extra});
const key=selection.key;
function setup(rows=[target(1),target(2)]){const c=selection.create();c.snapshot(rows);c.setDevices([{port:'A',connected:true},{port:'B',connected:true}]);return c;}

test('unchanged polls and unrelated telemetry preserve menu and copy context',()=>{
  const c=setup();c.select(key(target(1)),'replace',[]);c.assign(['B']);
  const before=[c.selectionSignature(),c.selectionRevision(),c.menuSignature()];
  c.snapshot([target(1,{rssi:-20}),target(2,{ssid:'Other changed',rssi:-90}),target(3)]);
  c.setDevices([{port:'B',connected:true},{port:'A',connected:true}]);c.notice('Background message');
  assert.deepEqual([c.selectionSignature(),c.selectionRevision(),c.menuSignature()],before);
});
test('copy context detects selected literal changes and change-then-return',()=>{
  const c=setup();c.select(key(target(1)),'replace',[]);const signature=c.selectionSignature(),epoch=c.selectionRevision();
  c.snapshot([target(1,{ssid:'Changed literal'}),target(2)]);assert.notEqual(c.selectionSignature(),signature);
  c.snapshot([target(1),target(2)]);assert.equal(c.selectionSignature(),signature);assert(c.selectionRevision()>epoch);
  const next=c.selectionRevision();c.clear();c.select(key(target(1)),'replace',[]);assert(c.selectionRevision()>next);
});
test('device and draft changes retire menu context independently from literal copy context',()=>{
  const c=setup();c.select(key(target(1)),'replace',[]);const copy=c.selectionSignature(),menu=c.menuSignature();
  c.assign(['B']);assert.equal(c.selectionSignature(),copy);assert.notEqual(c.menuSignature(),menu);
  const draftMenu=c.menuSignature();c.setDevices([{port:'A',connected:true}]);assert.notEqual(c.menuSignature(),draftMenu);
  assert.equal(c.selectionSignature(),copy);assert.equal(c.getDraft('B').available,false);
});
test('auth loss and restoration do not revive an earlier copy epoch',()=>{
  const c=setup();c.select(key(target(1)),'replace',[]);const signature=c.selectionSignature(),epoch=c.selectionRevision();
  c.authLoss();c.snapshot([target(1),target(2)]);c.select(key(target(1)),'replace',[]);
  assert.equal(c.selectionSignature(),signature);assert(c.selectionRevision()>epoch);
});

test('keys distinguish types, exact spelling and duplicate labels without SSID or row ordinals',()=>{
  const rows=[target(1),target(2),target(1,{target_type:'ble'}),target(1,{mac:'AA:00:00:00:00:01'}),target(1,{mac:'aa:00:00:00:00:01'})];
  const c=setup(rows);c.selectVisible(rows.map(key));assert.equal(c.view().selected.length,5);
  rows.reverse();c.snapshot(rows.map(r=>({...r,ssid:'renamed',rssi:-20})));assert.equal(c.view().selected.length,5);assert(c.fresh());
});
test('snapshot, view and per-device draft records are detached',()=>{
  const rows=[target(1)],c=setup(rows);c.select(key(rows[0]),'replace',[]);c.assign(['A','B']);rows[0].ssid='external';
  const v=c.view();v.selected[0].record.ssid='view';v.drafts[0].targets[0].record.mac='bad';
  const d=c.getDraft('A');d.targets[0].record.ssid='draft';assert.equal(c.getDraft('A').targets[0].record.ssid,'Same name');
  assert.equal(c.getDraft('B').targets[0].record.mac,target(1).mac);
});
test('toggle, replace and shift use current visible order; checkbox state is not an extra selection',()=>{
  const rows=[target(1),target(2),target(3),target(4)],c=setup(rows),order=rows.map(key);
  c.select(order[1],'replace',order);c.select(order[3],'range',order);assert.deepEqual(c.view().selected.map(e=>e.key),order.slice(1));
  c.select(order[2],'toggle',order);assert.deepEqual(c.view().selected.map(e=>e.key),[order[1],order[3]]);
  c.select(order[0],'replace',order);assert.deepEqual(c.view().selected.map(e=>e.key),[order[0]]);
});
for(const mode of ['range','visible','toggle'])test('128 cap refuses '+mode+' atomically',()=>{
  const rows=Array.from({length:129},(_,i)=>target(i)),c=setup(rows),order=rows.map(key);
  c.selectVisible(order.slice(0,128));const before=c.view().selected;
  const ok=mode==='visible'?c.selectVisible(order):c.select(order[128],mode,order);
  assert.equal(ok,false);assert.deepEqual(c.view().selected,before);assert.match(c.view().message,/128/);
});
test('full 5000-row snapshot is bounded independently from selected cap',()=>{
  const rows=Array.from({length:5000},(_,i)=>target(i)),c=setup(rows);assert.equal(c.selectVisible(rows.map(key)),false);assert.equal(c.view().selected.length,0);
  c.selectVisible(rows.slice(-128).map(key));assert.equal(c.view().selected.length,128);assert.throws(()=>c.snapshot([...rows,target(6000)]));
});
for(const replacement of ['missing','timestamp'])test(replacement+' latches stale selection and draft until explicit reselection and reassignment',()=>{
  const r=target(1),c=setup([r]);c.select(key(r),'replace',[]);c.assign(['A']);
  c.snapshot(replacement==='missing'?[]:[{...r,timestamp:'2026-09-06T12:00:01Z'}]);c.snapshot([r]);
  assert.equal(c.view().selected[0].stale,true);assert.equal(c.getDraft('A').targets[0].stale,true);assert.equal(c.copyValue('mac'),null);
  c.select(key(r),'toggle',[]);assert(c.fresh());assert(c.getDraft('A').targets[0].stale);c.assign(['A']);assert(!c.getDraft('A').targets[0].stale);
});
test('source disconnect is observed and cannot silently revive after reconnect',()=>{
  const r=target(1),c=setup([r]);c.select(key(r),'replace',[]);c.assign(['B']);c.setDevices([{port:'B',connected:true}]);
  c.setDevices([{port:'A',connected:true},{port:'B',connected:true}]);c.snapshot([r]);assert(c.view().selected[0].stale);assert(c.getDraft('B').targets[0].stale);
});
test('draft membership is frozen; disconnected assigned device remains unavailable after returning',()=>{
  const r=target(1),c=setup([r]);c.select(key(r),'replace',[]);c.assign(['B']);
  c.setDevices([{port:'A',connected:true},{port:'B',connected:true},{port:'C',connected:true}]);assert.deepEqual(c.view().drafts.map(d=>d.port),['B']);
  c.setDevices([{port:'A',connected:true}]);c.setDevices([{port:'A',connected:true},{port:'B',connected:true}]);assert.equal(c.getDraft('B').available,false);
  assert(c.assign(['B']));assert.equal(c.getDraft('B').available,true);
});
test('an invalid mixed device assignment leaves the preceding frozen draft intact',()=>{
  const r=target(1),c=setup([r]);c.select(key(r),'replace',[]);c.assign(['A']);assert.equal(c.assign(['A','missing']),false);assert.deepEqual(c.view().drafts.map(d=>d.port),['A']);
});
test('local selection clear/remove and draft clear are separate operations',()=>{
  const rows=[target(1),target(2)],c=setup(rows);c.selectVisible(rows.map(key));c.assign(['A']);c.remove(key(rows[0]));assert.equal(c.view().selected.length,1);
  c.clear();assert.equal(c.view().selected.length,0);assert.equal(c.getDraft('A').targets.length,2);c.clearDrafts();assert.equal(c.getDraft('A'),null);
});
test('actual MAC/SSID copying never promotes indexes, tag UIDs or BLE names',()=>{
  const cases=[target(1,{mac:'idx:3'}),target(1,{target_type:'nfc'}),target(1,{target_type:'ble',ssid:'name'}),target(1,{ssid:''})];
  for(const r of cases){const c=setup([r]);c.select(key(r),'replace',[]);if(r.target_type!=='ble')assert.equal(c.copyValue('mac'),r.ssid===''?r.mac:null);if(r.target_type==='ble'||!r.ssid)assert.equal(c.copyValue('ssid'),null);}
  const rows=[target(1,{ssid:'<img src=x>\nssid'}),target(2,{ssid:'literal "label"'})],c=setup(rows);c.selectVisible(rows.map(key));
  assert.equal(c.copyValue('ssid'),rows.map(r=>r.ssid).join('\n'));assert.equal(c.copyValue('mac'),rows.map(r=>r.mac).join('\n'));
});
test('ambiguous duplicate key is unavailable, and a missing timestamp cannot be selected',()=>{
  const r=target(1),c=setup([r,{...r,ssid:'other'},target(2,{timestamp:''})]);assert.equal(c.eligible(key(r)),false);assert.equal(c.eligible(key(target(2))),false);assert.equal(c.select(key(r),'replace',[]),false);
});
test('auth loss clears local identities and drafts; a later snapshot starts empty',()=>{
  const r=target(1),c=setup([r]);c.select(key(r),'replace',[]);c.assign(['A']);c.authLoss();
  assert.equal(c.view().authorized,false);assert.deepEqual(c.view().selected,[]);assert.deepEqual(c.view().drafts,[]);assert.equal(c.eligible(key(r)),false);
  c.snapshot([r]);assert.deepEqual(c.view().selected,[]);assert.deepEqual(c.view().drafts,[]);
});
test('a rendering observer failure cannot undo authoritative auth-loss clearing',()=>{
  const c=selection.create({onChange(){throw Error('render');}}),r=target(1);c.snapshot([r]);c.select(key(r),'replace',[]);c.authLoss();assert.deepEqual(c.view().selected,[]);
});
test('snapshot read model retains API timestamp and emits explicit auth-loss metadata',async()=>{
  const events=[],values=[ [target(1)], Promise.reject({status:401}) ];values[1].catch(()=>{});
  const c=snapshots.create({load:()=>values.shift(),onSnapshot:(rows,event)=>events.push({rows,event}),onStatus:()=>{}});
  assert.equal(await c.refresh(),true);assert.equal(events[0].rows[0].timestamp,stamp);assert.equal(events[0].event.kind,'snapshot');
  assert.equal(await c.refresh(),false);assert.equal(events[1].event.kind,'auth-loss');assert.deepEqual(events[1].rows,[]);
});
for(const invalid of ['2026-02-30T00:00:00Z','2026-09-06','2026-09-06T24:00:00Z','2026-09-06T00:00:00+99:00','2026-13-01T00:00:00Z','0000-01-01T00:00:00Z',7,true,{},'x'.repeat(41)])test('invalid timestamp is refused atomically: '+JSON.stringify(invalid),()=>{
  assert.equal(selection.timestamp(invalid),false);assert.throws(()=>snapshots.validate([target(1),target(2,{timestamp:invalid})]));
});
test('valid leap day and microseconds preserve exact incarnation text',()=>{
  for(const s of ['2024-02-29T23:59:59.123456Z',stamp,'2026-01-01T00:00:00-07:00'])assert.equal(snapshots.validate([target(1,{timestamp:s})])[0].timestamp,s);
});
