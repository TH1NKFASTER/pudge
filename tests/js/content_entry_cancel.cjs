'use strict';
const lifecycleTimeout=setTimeout(()=>{console.error('Review lifecycle test timed out');process.exit(1);},5000);
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(process.argv[2],'utf8');
const source=html.slice(html.indexOf('async function openLightNovel('),html.indexOf('async function loadLightNovelChapter('));
const deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return {resolve,promise};};
function harness(readerOpen=false){
  const classes=new Set(readerOpen?['open']:[]);
  const shell={classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x)}};
  const book=deferred(),review=deferred();
  const previousBook={id:90};
  const ui={page:'current',lnBook:previousBook,lnAudioFirstEntry:true};
  const context={ui,installLnSelectionDiagnostics(){},$:()=>shell,
    window:{PudgeReviewGate:{require:()=>review.promise}},pywebview:{api:{light_novel_open:()=>book.promise}}};
  vm.createContext(context);vm.runInContext(source,context);
  return {context,shell,book,review,ui,previousBook};
}
(async()=>{
  for(const previouslyOpen of [false,true]){
    const h=harness(previouslyOpen);
    const pending=h.context.openLightNovel(7,{pageOnOpen:'lightnovels'});
    assert.equal(h.shell.classList.contains('open'),previouslyOpen,'metadata fetch must retain the previous screen');
    h.book.resolve({id:7,current_chapter:1});
    await Promise.resolve();await Promise.resolve();
    assert.equal(h.shell.classList.contains('open'),previouslyOpen,'reader must not open beneath the review gate');
    h.review.resolve(false);await pending;
    assert.equal(h.shell.classList.contains('open'),previouslyOpen);
    assert.equal(h.ui.page,'current');
    assert.equal(h.ui.lnAudioFirstEntry,true);
    assert.equal(h.ui.lnBook,h.previousBook,'cancel preserves the previous reader');
  }
  const manga=fs.readFileSync(process.argv[3],'utf8');
  const openManga=manga.slice(manga.indexOf('  async function openBook('),manga.indexOf('  function closeReader('));
  const book=deferred(),review=deferred(),previousState={books:[]},previousBook={id:90};
  const context={bookOpenGeneration:0,state:previousState,currentBook:previousBook,
    API:()=>({manga_state:()=>book.promise}),window:{PudgeReviewGate:{require:()=>review.promise}}};
  vm.createContext(context);vm.runInContext(openManga,context);
  const pending=context.openBook(7,{pageOnOpen:'manga'});
  book.resolve({books:[{id:7,position:23}]});
  await Promise.resolve();await Promise.resolve();
  assert.equal(context.currentBook,previousBook);
  assert.equal(context.state,previousState);
  review.resolve(false);await pending;
  assert.equal(context.currentBook,previousBook,'cancel retains the previous manga book');
  assert.equal(context.state,previousState);
  console.log('Content entry cancellation: PASS');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>clearTimeout(lifecycleTimeout));
