// Exercise the actual page handlers with delayed responses, without model calls.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../web/static/judge.html'),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const handlers=script.slice(script.indexOf('function show(n)'),script.indexOf("$('play').onclick"));

function page(){
  const nodes={},requests=[],rendered=[];
  const context=vm.createContext({
    $:id=>nodes[id]??=( {style:{},disabled:false,textContent:''} ),
    document:{querySelectorAll:()=>[]},
    fetch:(_url,options)=>new Promise((resolve,reject)=>requests.push({resolve,reject,body:JSON.parse(options.body)})),
    renderResult:r=>rendered.push(r),
  });
  vm.runInContext(`let index=0,free=false,rule={id:'liner-rule'},ruleRequest=0,ruleBusy=false;
    const D={real:{reference:{demo:'ref.mp4'}},reference:{demo:'ref.mp4'}};
    const steps=Array.from({length:5},(_,i)=>({name:'step '+i,dataset:'real',time:'0:00',title:'Step '+i,text:'demo',source:'saved',ref:'ref',att:'attempt'}));
    function row(){return {verdict:'example-'+index,demo:'attempt.mp4'};}
    ${handlers}`,context);
  const state=()=>vm.runInContext('({free,index,ruleBusy})',context);
  const resolve=(i,verdict='changed')=>requests[i].resolve({ok:true,json:async()=>({pack_order:{verdict}})});
  context.show(1);
  return {context,nodes,requests,rendered,state,resolve};
}

test('inline script parses',()=>{new vm.Script(script);});

test('rule becomes free only after success and can be restored',async()=>{
  const p=page();const first=p.context.changeRule();
  assert.equal(p.state().free,false);
  assert.equal(p.nodes.toggleRule.disabled,true);
  assert.equal(p.requests[0].body.overrides['liner-rule'],'free');
  p.resolve(0,'pass_notes');await first;
  assert.equal(p.state().free,true);
  assert.equal(p.nodes.ruleStatus.textContent,'Human override applied.');
  const second=p.context.changeRule();p.resolve(1,'fail');await second;
  assert.equal(p.requests[1].body.overrides['liner-rule'],'required');
  assert.equal(p.state().free,false);
  assert.equal(p.rendered.at(-1).verdict,'fail');
});

test('double clicks do not submit two requests; navigation discards a slow result',async()=>{
  const p=page();const pending=p.context.changeRule();await p.context.changeRule();
  assert.equal(p.requests.length,1);
  p.context.show(2);p.resolve(0,'pass_notes');await pending;
  assert.equal(p.rendered.at(-1).verdict,'example-2');
  assert.equal(p.state().free,false);
  assert.equal(p.nodes.ruleStatus.textContent,'');
});

test('an old response cannot replace a newer result after returning to the example',async()=>{
  const p=page();const old=p.context.changeRule();
  p.context.show(2);p.context.show(1);
  const current=p.context.changeRule();p.resolve(1,'new-result');await current;
  p.resolve(0,'old-result');await old;
  assert.equal(p.rendered.at(-1).verdict,'new-result');
  assert.equal(p.state().free,true);
  assert.equal(p.nodes.toggleRule.disabled,false);
});

test('network failure preserves the last confirmed rule and permits retry',async()=>{
  const p=page();const pending=p.context.changeRule();p.requests[0].reject(new Error('offline'));await pending;
  assert.equal(p.state().free,false);
  assert.equal(p.state().ruleBusy,false);
  assert.equal(p.nodes.toggleRule.disabled,false);
  assert.match(p.nodes.ruleStatus.textContent,/try again/);
  const retry=p.context.changeRule();p.resolve(1);await retry;
  assert.equal(p.state().free,true);
});
