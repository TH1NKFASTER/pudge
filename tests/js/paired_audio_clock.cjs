'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const web = process.argv[2];
let now = 0;
const context = {performance:{now:()=>now}, console};
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(web, 'paired_audio_clock.js'), 'utf8'), context);
const {createClock, offsetAtTime} = context.PudgePairedAudioClock;
const clock = createClock();
clock.reset({position:10, playing:true, speed:1});
now=200;
assert.equal(clock.current(),10.2);
clock.reconcile({position:10.1,playing:true,speed:1});
now=400;
assert(Math.abs(clock.current()-10.4)<1e-9);
clock.reconcile({position:10.05,playing:true,speed:1});
assert(Math.abs(clock.current()-10.4)<1e-9,'ordinary poll jitter must not rewind');
clock.reconcile({position:10.3,playing:false,speed:1});
now=20000;
assert.equal(clock.current(),10.3,'paused clock must not advance');
clock.reset({position:100,playing:true,speed:2});
now+=500;
assert.equal(clock.current(),101);
// Positive poll drift is reconciled across frames, without jumping on receipt.
const beforePoll=clock.current();
clock.reconcile({position:beforePoll+.3,playing:true,speed:2});
assert(Math.abs(clock.current()-beforePoll)<1e-9,'poll response must not jump the visual clock');
now+=100;
assert(clock.current()>beforePoll+.2&&clock.current()<beforePoll+.31);
const state={anchor_window:{activity_clock:true,path:[{time:0,offset:0},{time:8,offset:8}],
  activity:[{start:0,end:2},{start:6,end:8}]}};
assert.equal(offsetAtTime(state,1),2);
assert.equal(offsetAtTime(state,3),4);
assert.equal(offsetAtTime(state,5),4,'silent interval holds the same word');
assert.equal(offsetAtTime(state,7),6);
assert.equal(offsetAtTime({...state,anchor_window:{...state.anchor_window,activity:[]}},3),3);

(async()=>{
  const html=fs.readFileSync(path.join(web,'index.html'),'utf8');
  const source=html.slice(html.indexOf('async function openLightNovel('),html.indexOf('async function loadLightNovelChapter('));
  const nodes=new Map(), calls=[];
  context.$=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',innerHTML:'',value:'',dataset:{},classList:{add(){},contains(){return false;},remove(){}}});return nodes.get(id);};
  context.ui={lnChapterCacheGeneration:0,lnChapterPayloadCache:new Map(),lnState:{settings:{}}};
  context.requestAnimationFrame=callback=>callback();
  context.escapeHtml=String;
  for(const name of ['installLnSelectionDiagnostics','persistReadTogetherSession','cancelLnAutoBookmark',
    'populateLnReaderAppearance','applyLnReaderSettings','syncLnFinishButton','ensureLnPairedControls',
    'syncLnChapterPicker','syncLnPairedTray','lnPairedTransportClockReset','pollLnPaired'])context[name]=(...args)=>calls.push({name,args});
  context.PudgeReviewGate={require:async()=>true};
  context.loadLightNovelChapter=async(...args)=>calls.push({name:'load',args});
  context.applyLnPairedPosition=async(...args)=>calls.push({name:'apply',args});
  // Only consumer/reader-context notifications are allowed; any transport or
  // state call during a warm handoff is a regression.
  const contextCalls=[];
  context.pywebview={api:new Proxy({}, {get:(_o,name)=>{
    if(name==='audiobook_review_context'||name==='audiobook_reader_context')return async(...args)=>{contextCalls.push({name,args});return {ok:true};};
    return ()=>{throw new Error('Unexpected API call during warm handoff: '+String(name));};}})};
  vm.runInContext(source, context);
  now=1000;
  const chapter={chapter_index:2,paragraphs:['甲乙'],tokens:[[]]};
  const book={id:7,title:'Novel',chapters:[{chapter_index:2,title:'Chapter 3'}],current_chapter:0,current_offset:.4};
  const paired={audiobook_id:5,ln_chapter_index:2,position:4,playing:true,player_running:true,speed:1,
    anchor_window:{path:[{time:4,offset:0},{time:6,offset:4}]}};
  await context.openLightNovel(7,{readTogether:true,audiobookId:5,bookPromise:Promise.resolve(book),
    audioBook:{id:5},pairedState:paired,pairedAt:500,chapterPayload:chapter});
  const load=calls.find(row=>row.name==='load');
  assert.deepEqual(JSON.parse(JSON.stringify(load.args)),[2,null,{audioFollow:true}],'handoff must open the audio chapter, not the stored bookmark');
  const apply=calls.find(row=>row.name==='apply');
  assert.equal(apply.args[0].position,4.5,'audio continues while opening the reader');
  assert.equal(apply.args[0].chapter_char_offset_exact,1);
  assert(context.ui.lnChapterPayloadCache.has('7:2'),'prepared chapter is reused');
  assert.equal(context.ui.lnPairedTransportStartupWatch,false,'existing audio is not treated as a fresh start');
  assert.deepEqual(JSON.parse(JSON.stringify(contextCalls.find(row=>row.name==='audiobook_review_context').args)),[5,'ln'],'reader becomes the review consumer');
  assert.equal(contextCalls.find(row=>row.name==='audiobook_reader_context').args[0].audiobook_id,5);
  assert.equal(context.ui.lnPairedExpanded,true);
  console.log('paired clock and reader handoff: PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
