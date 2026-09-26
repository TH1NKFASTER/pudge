'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const elements = new Map();
const periods = ['7d', '30d', 'year', 'all', 'custom'].map(value => ({dataset:{statsPeriod:value}, classList:{toggle(){}}}));
const tabs = ['overview','works','journal'].map(value => ({dataset:{statsTab:value}, classList:{toggle(){}}}));
const handlers = {};
let bodyWrites = 0;
class Element {
  constructor(id){this.id=id;this.dataset={};this.value='';this.hidden=false;this._html='';this.classList={toggle(){}};}
  get innerHTML(){return this._html;}
  set innerHTML(value){this._html=String(value);if(this.id==='statsBody')bodyWrites++;}
  setAttribute(name,value){this[name]=value;}
  addEventListener(type,callback){handlers[type]=callback;}
  querySelector(query){return query==='.stats-shell'&&this._html.includes('stats-shell')?{}:null;}
}
for(const id of ['statisticsContent','statsBody','statsStatus','statsKind','statsMedia','statsCustomDates','statsStartDate','statsEndDate','statsModalHost']) elements.set(id,new Element(id));
const document = {
  documentElement:{lang:'en'},head:{appendChild(){}},hidden:false,
  hasFocus(){return true;},
  getElementById(id){return elements.get(id)||null;},
  createElement(){return new Element('pudgeStatisticsStyle');},
  addEventListener(){},
  querySelectorAll(selector){return selector==='[data-stats-period]'?periods:selector==='[data-stats-tab]'?tabs:[];},
  querySelector(selector){return selector==='[data-stats-more]'&&elements.get('statsBody').innerHTML.includes('data-stats-more')?{disabled:false}:null;},
};
const pending=[];
const calls=[];
const api={
  consumption_statistics_query(scope,limit,offset){
    calls.push({scope:{...scope},limit,offset});
    return new Promise((resolve,reject)=>pending.push({resolve,reject,scope,offset}));
  },
  consumption_statistics_delete_history(){return Promise.resolve({ok:true});},
};
const window={pywebview:{api},addEventListener(){},pudgeConfirm(){return Promise.resolve(true)},toast(){}};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'), {
  window,document,setInterval(){},performance:{now:()=>0},console,
}, {filename:'statistics.js'});
const stats=window.PudgeStatistics;
const body=elements.get('statsBody');
function payload(n,rows=[],total=rows.length){return {summary:{total_seconds:n,active_days:1,sessions:1},days:[],breakdown:{},works:[],journal:rows,journal_total:total,media_options:[],timezone:{label:'local'}};}
function row(id){return {id,type:'session',title:'Work '+id,kind:'manga',start_utc:1000,end_utc:1010,seconds:10};}
async function event(type,key,value){const element={dataset:{[key]:value}};return handlers.click({target:{closest(selector){return selector==='[data-'+key.replace(/[A-Z]/g,c=>'-'+c.toLowerCase())+']'||(key==='statsDeleteHistory'&&selector==='#statsDeleteHistory')?element:null;}}});}
async function settle(){for(let i=0;i<5;i++)await Promise.resolve();}
(async()=>{
  const first=stats.load();const duplicate=stats.load();assert.equal(calls.length,1,'single-flight initial request');
  pending.shift().resolve(payload(11,[row(1)],3));await Promise.all([first,duplicate]);
  assert.equal(stats.state.payload.summary.total_seconds,11);
  const before=bodyWrites;
  await event('click','statsPeriod','30d');await event('click','statsTab','overview');
  assert.equal(calls.length,1,'active scope/tab must be no-op');assert.equal(bodyWrites,before,'active scope/tab must preserve DOM');
  const old=event('click','statsPeriod','7d');const newer=event('click','statsPeriod','year');
  assert.equal(calls.length,3);
  const oldPeriodRequest=pending.shift(),newPeriodRequest=pending.shift();
  newPeriodRequest.resolve(payload(99));await newer;
  oldPeriodRequest.resolve(payload(77));await old;
  assert.equal(stats.state.payload.summary.total_seconds,99,'out of order response must be ignored');
  assert.equal(stats.state.scope.period,'year');
  await event('click','statsTab','journal');
  const reset=event('click','statsPeriod','all');pending.shift().resolve(payload(15,[row(1),row(2)],5));await reset;
  const more=event('click','statsMore','');
  const double=event('click','statsMore','');
  assert.equal(calls.length,5,'double more must issue one request');
  assert.equal(calls[4].offset,2);
  pending.shift().resolve(payload(15,[row(2),row(3)],5));await Promise.all([more,double]);
  assert.equal(JSON.stringify(stats.state.journalRows.map(x=>x.id)),'[1,2,3]');
  assert.equal(stats.state.journalOffset,4,'offset must count raw rows even if overlapping');
  assert.ok(body.innerHTML.includes('Work 1')&&body.innerHTML.includes('Work 3'));
  const oldMore=event('click','statsMore','');
  const day=event('click','statsDay','2026-09-21');
  assert.equal(calls[6].offset,0,'changing to one day resets offset');
  const priorMoreRequest=pending.shift(),dayRequest=pending.shift();
  dayRequest.resolve(payload(25,[row(9)]));await day;
  priorMoreRequest.resolve(payload(50,[row(8)]));await oldMore;
  assert.equal(JSON.stringify(stats.state.journalRows.map(x=>x.id)),'[9]');
  assert.equal(stats.state.payload.summary.total_seconds,25);
  const stale=stats.load();
  const mutation=event('click','statsDeleteHistory','');await settle();
  assert.equal(stats.state.payload,null,'successful deletion clears old totals before re-query');
  assert.equal(stats.state.journalRows.length,0,'successful deletion clears old journal immediately');
  assert.equal(body.innerHTML.includes('Work 9'),false,'deleted journal entry is not displayed while refresh is pending');
  assert.equal(calls.length,9);
  const preMutationRequest=pending.shift(),postMutationRequest=pending.shift();
  postMutationRequest.resolve(payload(0,[]));await mutation;
  preMutationRequest.resolve(payload(999,[row(99)]));await stale;
  assert.equal(stats.state.payload.summary.total_seconds,0,'pre-mutation response must not resurrect old history');
  const failed=stats.load();pending.shift().reject(new Error('offline'));await failed;
  assert.equal(stats.state.payload.summary.total_seconds,0,'refresh error must retain last success');
  assert.ok(elements.get('statsStatus').innerHTML.includes('Retry'));
  assert.equal(stats.state.loading,false);
  const partial=stats.load();
  pending.shift().resolve(payload(50,[row(1)],5));await partial;
  assert.equal(stats.state.journalHasMore,true);
  const lastPage=stats.load({more:true});
  pending.shift().resolve(payload(50,[],5));await lastPage;
  assert.equal(stats.state.journalHasMore,false);
  assert.equal(body.innerHTML.includes('data-stats-more'),false,'empty page hides More');
  const callsBefore=calls.length;
  await stats.load({more:true});
  assert.equal(calls.length,callsBefore,'exhausted journal must not repeatedly query');
  console.log('pagination exhaustion: PASS');
  // Reloading while a prior More request is pending must not leave pagination
  // permanently disabled; the late response must not overwrite the reload.
  const firstPageWithDuplicates=stats.load();
  pending.shift().resolve(payload(70,[row(10),row(10)],5));await firstPageWithDuplicates;
  assert.deepEqual(stats.state.journalRows.map(x=>x.id),[10],'first page deduplicates repeated server rows');
  assert.equal(stats.state.journalOffset,2,'first-page offset counts raw server rows');
  const repeatedPage=stats.load({more:true});
  pending.shift().resolve(payload(70,[row(11),row(11)],5));await repeatedPage;
  assert.deepEqual(stats.state.journalRows.map(x=>x.id),[10,11],'More deduplicates within the same response');
  assert.equal(stats.state.journalOffset,4,'More offset counts raw server rows');
  const oldMoreRequest=stats.load({more:true});
  const fullReload=stats.load();
  assert.equal(stats.state.loadingMore,false,'full reload releases the old More spinner');
  const lateMore=pending.shift(),newPage=pending.shift();
  newPage.resolve(payload(80,[row(12)],5));await fullReload;
  assert.equal(stats.state.loadingMore,false,'reload completes without stranded pagination state');
  lateMore.resolve(payload(70,[row(99)],5));await oldMoreRequest;
  const afterReloadMore=stats.load({more:true});
  assert.equal(calls.at(-1).offset,1,'new pagination starts from new page');
  pending.shift().resolve(payload(80,[row(13)],5));await afterReloadMore;
  assert.deepEqual(stats.state.journalRows.map(x=>x.id),[12,13],'stale More never leaks into fresh results');
  console.log('pagination dedupe and reload race: PASS');
  console.log('statistics g7 async scenarios: PASS');
})().catch(error=>{console.error(error);process.exitCode=1});
