'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const start = src.indexOf('  function mangaTokenHitboxes(regionNode, region) {');
const end = src.indexOf('  function mangaTokenAtPoint(', start);
assert.ok(start > 0 && end > start, 'functions must exist');
const lookup = vm.runInNewContext(`(() => { const MANGA_TOKEN_HIT_SLOP_PX=8; const mangaHitSurface = s => String(s || '');\n${src.slice(start, end)}\nreturn {mangaTokenHitboxes,mangaTokenHitAtPoint};})()`);
const node = {dataset:{rawX:'0.2',rawY:'0.3',rawWidth:'0.2',rawHeight:'0.3'},querySelectorAll(){return [{textContent:'日本'}, {textContent:'語'}];}};
const img = {getBoundingClientRect(){return {left:10,top:10,width:1000,height:1000};}};
const frame = {querySelector(sel){return sel==='img'?img:null;}};
const mokuro = {text:'日本語',provenance:{source:'mokuro'},geometry_status:'approximate'};
const hit = lookup.mangaTokenHitAtPoint(frame,node,mokuro,300,560);
assert.equal(hit?.virtualText,'日本語');
assert.equal(hit?.source,'mokuro-block-approximate-v1');
assert.equal(hit?.token,null);
assert.equal(lookup.mangaTokenHitAtPoint(frame,node,mokuro,900,560),null,'outside actual block must not hit');
assert.equal(lookup.mangaTokenHitboxes(node,{...mokuro,geometry_status:'unknown'}).length,0,'never guess geometry');
assert.equal(lookup.mangaTokenHitboxes(node,{...mokuro,provenance:{source:'vision'}}).length,0,'native geometry unchanged');
assert.equal(lookup.mangaTokenHitboxes({...node,dataset:{...node.dataset,rawWidth:'NaN'}},mokuro).length,0,'invalid geometry must not be clickable');
const lined = {...mokuro, provenance:{source:'mokuro',line_boxes:[
  {text:'日本',x:0.21,y:0.45,width:0.15,height:0.10},
  {text:'語',x:0.21,y:0.31,width:0.15,height:0.10},
]}};
assert.equal(lookup.mangaTokenHitAtPoint(frame,node,lined,300,510)?.virtualText,'日本','first actual line has its own lookup');
assert.equal(lookup.mangaTokenHitAtPoint(frame,node,lined,300,630)?.virtualText,'語','second actual line has its own lookup');
assert.equal(lookup.mangaTokenHitAtPoint(frame,node,lined,300,575),null,'blank gap between line rectangles must not open full block');
const outsideLine = {...mokuro,provenance:{source:'mokuro',line_boxes:[{text:'WRONG',x:0.8,y:0.3,width:0.1,height:0.1}]}};
assert.equal(lookup.mangaTokenHitboxes(node,outsideLine).length,1,'invalid line falls back only to observed parent block');
assert.equal(lookup.mangaTokenHitAtPoint(frame,node,outsideLine,850,600),null,'foreign line cannot make another bubble clickable');
const missingCoordinate = {...mokuro,provenance:{source:'mokuro',line_boxes:[{text:'WRONG',x:null,y:0.35,width:0.1,height:0.1}]}};
assert.equal(lookup.mangaTokenHitboxes(node,missingCoordinate)[0].virtualText,'日本語','null line coordinate must never fabricate an observed hitbox');
const crossing = {...mokuro,provenance:{source:'mokuro',line_boxes:[{text:'日本',x:0.18,y:0.45,width:0.05,height:0.10}]}};
const cropped = lookup.mangaTokenHitboxes(node,crossing)[0];
assert.ok(cropped.x>=0.2 && cropped.x+cropped.width<=0.4,'line click area is clipped to the imported block');

const dispatchStart = src.indexOf('  async function dispatchMangaVirtualStudyHit(');
const dispatchEnd = src.indexOf('  async function dispatchMangaStudyClick(',dispatchStart);
assert.ok(dispatchStart > 0 && dispatchEnd > dispatchStart);
const calls=[];
const context={window:{PudgeReadingTools:{study:{openText:async(...args)=>{calls.push(args);return true;}}}},
  DOMRect:undefined,currentStudyBackend:()=> 'jiten'};
const run = vm.runInNewContext(`(() => { ${src.slice(start,src.indexOf('  function mangaTokenAtPoint(',start))}
${src.slice(dispatchStart,dispatchEnd)}
return dispatchMangaVirtualStudyHit;})()`,{...context,MANGA_TOKEN_HIT_SLOP_PX:8,mangaHitSurface:s=>String(s||'')});
run(hit,frame).then(opened=>{
  assert.equal(opened,true);
  assert.equal(calls.length,1);
  assert.equal(calls[0][0],'日本語','study receives the full actual text block');
  assert.equal(calls[0][2].backend,'jiten');
  assert.ok(calls[0][1].width>0 && calls[0][1].height>0,'study receives real block geometry');
  console.log('mokuro block hit: PASS');
  console.log('mokuro observed line hit: PASS');
}).catch(error=>{console.error(error);process.exitCode=1});
