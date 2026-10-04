'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const deadline=setTimeout(()=>{console.error('Later acknowledgement timed out');process.exit(1);},5000);
function element(){
  const classes=new Set();
  return {hidden:true,children:[],dataset:{},style:{},listeners:{},
    classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x)},
    addEventListener(name,fn){this.listeners[name]=fn;},append(...children){this.children.push(...children);},
    replaceChildren(...children){this.children=children;},
    querySelectorAll(){return this.children.flatMap(row=>row.children).filter(row=>row.dataset.plannedReleaseWatch);}};
}
const nodes=new Map();const $=id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);};
let acknowledge;const calls=[],errors=[];
const context={console,ui:{lang:'en'},$,document:{createElement:element},setInterval(){},
  toast:error=>errors.push(error),t:(_key,data)=>data.error,
  pywebview:{api:{dismiss_planned_release_offers:ids=>{calls.push([...ids]);return new Promise(resolve=>{acknowledge=resolve;});}}}};
context.window=context;vm.createContext(context);
const html=fs.readFileSync(process.argv[2],'utf8');
vm.runInContext(html.slice(html.indexOf('const plannedReleaseUi='),html.indexOf('const anilistRelatedUi=')),context);
(async()=>{
  context.renderPlannedReleaseOffers([{media_id:3,title:'Synthetic release'}]);
  const closing=context.closePlannedReleaseOffers();
  assert.deepEqual(calls,[[3]]);
  assert.equal($('plannedReleaseClose').disabled,true);
  assert($('plannedReleaseBackdrop').classList.contains('open'),'wait for durable acknowledgement');
  await context.closePlannedReleaseOffers();assert.equal(calls.length,1);
  acknowledge({ok:true});await closing;
  assert.equal($('plannedReleaseBackdrop').hidden,true);
  context.renderPlannedReleaseOffers([{media_id:3,title:'Synthetic release'},{media_id:4,title:'New synthetic release'}]);
  assert.equal($('plannedReleaseList').children.length,1,'old snapshot does not reinsert acknowledged title');
  const failed=context.closePlannedReleaseOffers();acknowledge({ok:false});await failed;
  assert($('plannedReleaseBackdrop').classList.contains('open'));
  assert.equal(errors.length,1);assert.equal($('plannedReleaseClose').disabled,false);
  const retry=context.closePlannedReleaseOffers();acknowledge({ok:true});await retry;
  assert.deepEqual(calls,[[3],[4],[4]]);
  console.log('Later durable acknowledgement and retry: PASS');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>clearTimeout(deadline));
