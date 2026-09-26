'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const pop = {
  _html:'',
  style:{removeProperty(){}},
  classList:{_set:new Set(),add(x){this._set.add(x)},remove(x){this._set.delete(x)},contains(x){return this._set.has(x)}},
  getBoundingClientRect(){return {left:0,top:0,bottom:10,width:440,height:400}},
  querySelector(){return null},
  get innerHTML(){return this._html},
  set innerHTML(html){this._html = html;badge = html.includes('pudge-study-optimal') ? {hidden:true} : null;},
};
let badge = null;
let studyNodes = [];
const document = {
  documentElement:{lang:'en'},
  body:{appendChild(){}},
  addEventListener(){},
  createElement(){return pop},
  getElementById(id){return id === 'pudgeStudyCard' ? pop : null},
  querySelector(selector){return selector === '#pudgeStudyCard .pudge-study-optimal' ? badge : null},
  querySelectorAll(selector){return selector === '[data-pudge-study-token]' ? studyNodes : []},
};
let resolveStateA;
const window = {
  matchMedia(){return {matches:true}},
  innerWidth:1000,innerHeight:800,
  pywebview:{api:{
    study_decks:async()=>[],
    study_state:({word_id})=>word_id===6
      ? new Promise(resolve=>{resolveStateA=resolve})
      : Promise.resolve({ok:true,states:['new'],normalizedState:'new'}),
  }},
  addEventListener(){},
};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),{
  window,document,requestAnimationFrame(){},getComputedStyle(){return {}},
  setTimeout(){return 1},clearTimeout(){},console,Date,
}, {filename:'reading_tools.js'});
const study = window.PudgeReadingTools.study;
const words = Array.from({length:11},(_,i)=>`語${i+1}`);
const sentence = words.join(' ')+'。';
let offset=0;
const tokens = words.map((word,i)=>{
  const start=sentence.indexOf(word,offset);offset=start+word.length;
  return {wordId:i+1,readingIndex:0,surface:word,sentence,contextStart:start,contextEnd:offset,
    card:{states:i===5?['new']:['mature'],frequencyRank:i===5?1000:100,studyDeckIds:[]}};
});
const fakeClasses=()=>({add(){},remove(){}});
let rows=tokens.map(token=>({token,node:{classList:fakeClasses()}}));
assert.equal(study.evaluateOptimalTargets(rows,{frequencyLimit:1000}).has(tokens[5]),true,'eligible boundary');
assert.equal(study.applyOptimalHighlights(rows,{enabled:false}),0,'background setting only affects drawing');
assert.equal(study.evaluateOptimalTargets(rows,{frequencyLimit:1000}).has(tokens[5]),true);
// A duplicate representation of a known occurrence must not reach the 10-token threshold.
const nine=tokens.slice(0,9).map(token=>({token,node:{classList:fakeClasses()}}));
assert.equal(study.evaluateOptimalTargets([...nine,nine[0]],{frequencyLimit:1000}).size,0);
const duplicateUnknown={...tokens[5],card:{...tokens[5].card}};
assert.equal(study.evaluateOptimalTargets([...rows,{token:duplicateUnknown,node:{classList:fakeClasses()}}],{frequencyLimit:1000}).size,1);
tokens[4].card={states:['new'],frequencyRank:100,studyDeckIds:[]};
assert.equal(study.evaluateOptimalTargets(rows,{frequencyLimit:1000}).size,0,'multiple unknown occurrences');
tokens[4].card={states:['mature'],frequencyRank:100,studyDeckIds:[]};
tokens[5].card.frequencyRank=1001;
assert.equal(study.evaluateOptimalTargets(rows,{frequencyLimit:1000}).size,0,'rank above limit');
tokens[5].card.frequencyRank=1000;
async function renderAndOpen(){
  const payload={paragraphs:[sentence],vocabulary:tokens.map(t=>({...t.card,wordId:t.wordId,readingIndex:0})),
    tokens:[tokens.map(t=>({wordId:t.wordId,readingIndex:0,start:t.contextStart,end:t.contextEnd}))]};
  const html=study.renderParsedText(payload,{backend:'jiten'});
  const ids=[...html.matchAll(/data-pudge-study-token="([^"]+)"/g)].map(x=>x[1]);
  assert.equal(ids.length,11);
  studyNodes=ids.map(id=>({dataset:{pudgeStudyToken:id},classList:fakeClasses(),getBoundingClientRect(){return {left:0,top:0,bottom:10,width:40,height:15}}}));
  await study.openElement(studyNodes[5]);
  assert.equal(badge.hidden,false,'Jiten card for eligible word shows star');
  assert.match(pop.innerHTML,/role="img" aria-label="A good new word to learn in this context"/);
  assert.match(pop.innerHTML,/tabindex="0"/);
  study.applyOptimalHighlights(studyNodes.map((node,i)=>({node,token:tokens[i]})),{enabled:false});
  assert.equal(badge.hidden,false,'disabling background does not hide star');
  // The older A refresh must not change the card after B is opened.
  await study.openElement(studyNodes[4]);
  assert.equal(badge.hidden,true,'non-optimal word has no star');
  resolveStateA({ok:true,states:['mature'],normalizedState:'known'});
  await Promise.resolve();await Promise.resolve();
  assert.equal(badge.hidden,true,'late A refresh cannot alter B');
  study.close();
  const manga=study.renderParsedText(payload,{backend:'jiten',mediaContext:{kind:'manga'}});
  const mangaIds=[...manga.matchAll(/data-pudge-study-token="([^"]+)"/g)].map(x=>x[1]);
  studyNodes=mangaIds.map(id=>({dataset:{pudgeStudyToken:id},classList:fakeClasses(),getBoundingClientRect(){return {left:0,top:0,bottom:10,width:40,height:15}}}));
  await study.openElement(studyNodes[5]);
  assert.equal(badge.hidden,true,'manga context cannot claim N+1');
  console.log('Jiten N+1 study-card star: PASS');
}
renderAndOpen().catch(error=>{console.error(error);process.exitCode=1});
