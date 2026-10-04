'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const root=process.argv[2];
(async()=>{
 const html=fs.readFileSync(path.join(root,'pudge/web/index.html'),'utf8');
 const functionText=html.slice(html.indexOf('function pollLnPaired(){'),html.indexOf('\nasync function seekLnPairedRelative'));
 let timerId=0,calls=0,open=true;const timers=new Map();
 const ui={lnBook:{id:7,paired_audio:{}},lnPairedPollGeneration:0,lnPairedPollTimer:null};
 const ctx={ui,$:()=>({classList:{contains:()=>open}}),console:{error:()=>{}},setTimeout:(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId;},clearTimeout:id=>timers.delete(id),
  pywebview:{api:{light_novel_paired_state:async()=>{calls++;if(calls===1)throw Error('temporary');return {playing:true,position:10};}}},
  lnPairedTransportClockReconcile:()=>({position:10}),syncLnPairedTray:()=>{},cancelLnPairedInterpolation:()=>{},applyLnPairedPosition:async()=>{}};
 vm.createContext(ctx);vm.runInContext(functionText,ctx);ctx.pollLnPaired();
 async function tick(){const [id,row]=timers.entries().next().value;timers.delete(id);await row.fn();}
 await tick();assert.equal(calls,1);assert.equal(timers.size,1);await tick();assert.equal(calls,2);assert.equal(timers.size,1);
 open=false;await tick();assert.equal(timers.size,0);
 // Closing/changing generation while an API call is in flight must not restart polling.
 open=true;let resolve;ctx.pywebview.api.light_novel_paired_state=()=>new Promise(r=>resolve=r);ctx.pollLnPaired();
 const [id,row]=timers.entries().next().value;timers.delete(id);const pending=row.fn();ui.lnPairedPollGeneration++;resolve({playing:true});await pending;assert.equal(timers.size,0);
 // A fresh origin gets backend values; pending writes coalesce in order and clear is durable.
 const saved={},writes=[];let release;
 const win={__pudgeUiBootstrap:{schema:1,values:{'pudge.assistant.geometry.v1':{x:12}}},addEventListener:()=>{},
   localStorage:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}},pywebview:{api:{ui_preference_save:async(k,v)=>{writes.push([k,v]);if(writes.length===1)await new Promise(r=>release=r);saved[k]=v;}}}};
 const storeCtx={window:win,console,Map,Set,JSON,Promise};vm.createContext(storeCtx);vm.runInContext(fs.readFileSync(path.join(root,'pudge/web/persistent_ui.js'),'utf8'),storeCtx);
 const storage=win.PudgeUiStorage;assert.equal(storage.get('pudge.assistant.geometry.v1').x,12);
 storage.set('pudge.assistant.v1',{messages:[{content:'a'}]});storage.set('pudge.assistant.v1',{messages:[{content:'b'}]});storage.set('pudge.assistant.v1',null);release();await storage.flush();assert.equal(saved['pudge.assistant.v1'],null);assert.equal(writes.length,2);
 // A bridge failure must retry even when the user makes no further edits.
 let retryFn,attempts=0;win.setTimeout=fn=>{retryFn=fn;return 1;};win.clearTimeout=()=>{};
 win.pywebview.api.ui_preference_save=async(k,v)=>{attempts++;if(attempts===1)throw Error('temporary');saved[k]=v;};
 storeCtx.console={error:()=>{}};
 storage.set('pudge.readTogether.v1',{bookId:7});await storage.flush();
 assert.equal(attempts,1);assert(retryFn);retryFn();await storage.flush();
 assert.equal(saved['pudge.readTogether.v1'].bookId,7);assert.equal(attempts,2);
 console.log('LN retry, stale generation, new-origin preferences and ordered clear: OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
