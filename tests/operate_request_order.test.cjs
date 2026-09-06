const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../src/ui/web/static/reform.js'),'utf8');
const start=source.indexOf('    var commandGeneration = 0;'),end=source.indexOf('    sel.addEventListener("change"',start);
assert(start>=0 && end>start);
function setup(){
  const calls=[],paints=[],bindings=[];
  const context=vm.createContext({sel:{value:'A'},grid:{innerHTML:'Old A button'},fwEl:{textContent:'A firmware'},termEl:{},statusEl:{},
    unbindTerminalEl:()=>bindings.push(null),bindTerminal:p=>bindings.push(p),
    getJSON:url=>new Promise((resolve,reject)=>calls.push({url,resolve,reject})),
    renderCmdGrid:(groups,port)=>{paints.push({groups,port});context.grid.innerHTML='Buttons for '+port;}});
  vm.runInContext(source.slice(start,end),context);
  const load=port=>{context.sel.value=port;context.loadFor(port);};
  return {context,calls,paints,bindings,load};
}
async function flush(){for(let n=0;n<5;n++)await Promise.resolve();}
test('switching port clears old buttons and firmware before the request completes',()=>{
  const f=setup();f.load('B');assert.match(f.context.grid.innerHTML,/Loading/);assert.equal(f.context.fwEl.textContent,'—');
  assert.deepEqual(f.bindings,[null,'B']);assert.match(f.calls[0].url,/port=B$/);
});
test('late old-port success cannot replace current buttons or firmware',async()=>{
  const f=setup();f.load('A');f.load('B');f.calls[1].resolve({groups:['new'],firmware:'B firmware'});await flush();
  f.calls[0].resolve({groups:['old'],firmware:'A firmware'});await flush();
  assert.deepEqual(f.paints,[{groups:['new'],port:'B'}]);assert.equal(f.context.fwEl.textContent,'B firmware');
});
test('late failure cannot replace current success',async()=>{
  const f=setup();f.load('A');f.load('B');f.calls[1].resolve({groups:[],firmware:'B'});await flush();
  f.calls[0].reject(new Error('old failure'));await flush();assert.equal(f.context.grid.innerHTML,'Buttons for B');
});
test('A to B to A does not accept the first A request',async()=>{
  const f=setup();f.load('A');f.load('B');f.load('A');f.calls[0].resolve({groups:['old'],firmware:'old'});await flush();
  assert.equal(f.paints.length,0);assert.match(f.context.grid.innerHTML,/Loading/);
  f.calls[2].resolve({groups:['fresh'],firmware:'fresh'});await flush();assert.equal(f.paints[0].groups[0],'fresh');
});
test('disconnect invalidates the response and leaves no port-bound buttons',async()=>{
  const f=setup();f.load('A');f.load('');f.calls[0].resolve({groups:['old'],firmware:'old'});await flush();
  assert.equal(f.paints.length,0);assert.match(f.context.grid.innerHTML,/Connect a device/);assert.equal(f.context.fwEl.textContent,'—');
  assert.deepEqual(f.bindings,[null,'A',null]);
});
test('current failure is visible and does not restore old buttons',async()=>{
  const f=setup();f.load('B');f.calls[0].reject(new Error('offline'));await flush();assert.match(f.context.grid.innerHTML,/Could not load/);
});
