'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const document = {
  documentElement:{lang:'en',style:{setProperty(){}}},
  body:{appendChild(){}},
  addEventListener(){},
  createElement(){return {style:{},classList:{add(){},remove(){},contains(){return false}},set innerHTML(v){this._html=v},get innerHTML(){return this._html||''}}},
  getElementById(){return null},
  querySelector(){return null},
  querySelectorAll(){return []},
};
const window = {
  matchMedia(){return {matches:true}}, innerWidth:1000, innerHeight:800,
  pywebview:{api:{
    study_states:async()=>({ok:true,states:[{ok:true,wordId:42,readingIndex:0,states:['mature'],normalizedState:'learning',stale:false}]}),
    study_provider_capabilities:async()=>({configured:true,account_key:'jiten:test-account'}),
  }},
  addEventListener(){},dispatchEvent(){},
};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),{
  window,document,CustomEvent:function(){},requestAnimationFrame(){},getComputedStyle(){return {}},
  setTimeout(){return 1},clearTimeout(){},console,Date,
},{filename:'reading_tools.js'});
(async()=>{
  const study=window.PudgeReadingTools.study;
  const rows=await study.lookupLiveStates([[42,0]]);
  assert.equal(rows.length,1);
  assert.equal(rows[0].account_scope,'jiten:test-account','capability scope backfills mixed-version batch');

  const sentence='個人的には普段通りにしてくれていれば、それがベストだったのだが……。';
  const surfaces=['個人的','に','は','普段通り','に','して','くれて','いれば','それ','が','ベスト','だった','の','だが'];
  let cursor=0;
  const tokens=surfaces.map((surface,index)=>{
    const start=sentence.indexOf(surface,cursor); cursor=Math.max(cursor,start+surface.length);
    return {wordId:index+1,readingIndex:0,surface,sentence,contextStart:start,contextEnd:start+surface.length,
      card:{states:surface==='普段通り'?['new']:['mature'],frequencyRank:surface==='普段通り'?900:100,studyDeckIds:[]}};
  });
  const eligible=study.evaluateOptimalTargets(tokens.map(token=>({token})),{frequencyLimit:1000});
  assert.equal(eligible.has(tokens[3]),true,'普段通り is N+1 when its neighbours are known and rank is in policy');
  console.log('G16 live scope + N+1 sentence: PASS');
})().catch(error=>{console.error(error);process.exitCode=1});
