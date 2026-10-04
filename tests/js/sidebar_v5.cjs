'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const web=process.argv[2],index=fs.readFileSync(path.join(web,'index.html'),'utf8');
(async()=>{
  const close=index.slice(index.indexOf("    if(target.id==='lnReaderClose')"),index.indexOf("    if(target.dataset.lnAudioSeek"));
  assert(close.includes("target.id==='lnReaderClose'"));
  let cancelResolve;const calls=[];
  const state={audiobook_id:5,position:24,playing:true,player_running:true},book={id:6,paired_audio:{book:{id:5}}};
  const nodes=new Map();const $=id=>{if(!nodes.has(id))nodes.set(id,{classList:{remove(){}},replaceChildren(){}});return nodes.get(id);};
  const context={console,Promise,Number,ui:{lnBook:book,lnPairedState:state,lnChapterPayloadCache:new Map(),lnTokenMap:new Map(),lnReadTogether:{returnPage:'audiobooks',audiobookId:5}},
    $,window:{PudgeAssistant:{close(){}},PudgeSidebarCompanion:{acceptPairedState:(s,b)=>calls.push({state:s,book:b})},PudgeMedia:{loadAudio:async()=>{}}},
    pywebview:{api:{light_novel_cancel_reader_background:()=>new Promise(resolve=>{cancelResolve=resolve;}),light_novel_stop_paired:()=>{throw Error('Closing reader stopped playback');}}},
    document:{querySelector:()=>null},requestAnimationFrame:()=>{},setPage:page=>calls.push(page),
    storedReadTogetherSession:()=>null,persistReadTogetherSession:()=>{},closeLnFind(){},closeLnChapterPicker(){},cancelLnAutoBookmark(){},stopLnParsePoll(){},stopLnPairedPoll(){},hideLnTranslation(){}};
  vm.createContext(context);
  vm.runInContext(`globalThis.closeReader=async()=>{const target={id:'lnReaderClose'};${close}}`,context);
  const pending=context.closeReader();
  assert.equal(calls.length,1,'Sidebar handoff must precede server response');
  assert.equal(calls[0].state.playing,true);assert.equal(calls[0].state.position,24);assert.equal(calls[0].book.id,6);
  assert.equal(context.ui.lnPairedState,null);cancelResolve();await pending;
  assert(calls.includes('audiobooks'));
  console.log('reader close keeps audio running: PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
