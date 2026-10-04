'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const web = process.argv[2];
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
};
const card = id => ({wordId:id, readingIndex:0, wordTextPlain:'選ぶ',
  readings:[{readingIndex:0, text:'選ぶ', rubyText:'選[えら]ぶ'}],
  pitchAccents:['2'], meanings:['choose'], sourceDeckName:'Long source'});

function harness(api = {}) {
  const storage = new Map([['pudge.sidebarReview.interval.v1', 'continuous']]);
  const host = {hidden:false, dataset:{}, innerHTML:'', querySelector:() => null};
  const root = {isConnected:true, querySelector:selector => selector === '[data-sidebar-due]' ? host : null};
  const listeners = new Map();
  const context = {
    console, setTimeout:() => 1, clearTimeout() {}, requestAnimationFrame() {},
    Date, performance:{now:()=>1000}, cancelAnimationFrame() {}, crypto:{randomUUID:() => 'attempt'}, CSS:{escape:String},
    localStorage:{getItem:key => storage.get(key) || null, setItem:(key, value) => storage.set(key, value)},
    document:{documentElement:{lang:'en', classList:{contains:()=>false}}, body:{}, activeElement:null,
      addEventListener(name, callback) { listeners.set(name, [...(listeners.get(name) || []), callback]); },
      querySelector:() => null, querySelectorAll:() => [], getElementById:() => null},
    addEventListener() {}, ui:{lang:'en'}, toast() {},
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(web, 'jiten_words.js'), 'utf8'), context);
  vm.runInContext(fs.readFileSync(path.join(web, 'reading_tools.js'), 'utf8'), context);
  vm.runInContext(fs.readFileSync(path.join(web, 'paired_audio_clock.js'), 'utf8'), context);
  let source = fs.readFileSync(path.join(web, 'sidebar_companion.js'), 'utf8');
  const marker = '  g.PudgeSidebarCompanion = {';
  assert(source.includes(marker));
  source = source.replace(marker, `
    g.sidebarTest = {
      frontWordHtml, answerDetails, previousReviewHtml, paragraphPosition, liveWindow,
      checkDue, submitDue, undoLastSidebarReview, noteExternalReview, activeBook,
      handleKeydown, audioClick, renderDue, restoreSettings, intervalValue, reconcileAudioState, acceptAudioState,
      set(value) {
        if ('root' in value) root=value.root;
        if ('card' in value) dueCard=value.card;
        if ('queue' in value) dueQueue=value.queue;
        if ('audio' in value) audioState=value.audio;
        if ('previous' in value) previousReview=value.previous;
        if ('revealed' in value) dueRevealed=value.revealed;
      },
      state:()=>({card:dueCard, queue:dueQueue, revealed:dueRevealed, busy:dueBusy, previous:previousReview})
    };
${marker}`);
  vm.runInContext(source, context);
  context.pywebview = {api};
  const test = context.sidebarTest;
  test.set({root});
  return {test, context, host, listeners};
}
const flush = async () => { for (let i=0; i<8; i++) await Promise.resolve(); };

(async () => {
  // Empty is shown only after a successful completed fetch, including refills.
  const pendingDue = deferred();
  const coldDue = harness({sidebar_due_review_cards:() => pendingDue.promise});
  const coldFetch = coldDue.test.checkDue();
  assert.match(coldDue.host.innerHTML, /Loading cards/);
  assert.doesNotMatch(coldDue.host.innerHTML, /No cards due/);
  pendingDue.resolve({cards:[]});
  await coldFetch;
  assert.match(coldDue.host.innerHTML, /No cards due/);
  const refillDue = deferred();
  coldDue.context.pywebview.api.sidebar_due_review_cards=() => refillDue.promise;
  const refetch = coldDue.test.checkDue();
  assert.match(coldDue.host.innerHTML, /Loading cards/);
  refillDue.resolve({cards:[card(42)]});
  await refetch;
  assert.equal(coldDue.test.state().card.wordId,42);
  const missingApi = harness();
  await missingApi.test.checkDue();
  assert.match(missingApi.host.innerHTML, /Could not load cards/);
  assert.doesNotMatch(missingApi.host.innerHTML, /No cards due/);
  const malformed = harness({sidebar_due_review_cards:async()=>({})});
  await malformed.test.checkDue();
  assert.match(malformed.host.innerHTML, /Could not load cards/);

  // Reading stays attached to the front even without rubyText and for long words.
  const h = harness();
  const front = h.test.frontWordHtml(card(1), true);
  assert.match(front, /sidebar-due-front-reading[^>]*><span class="pudge-inline-pitch/);
  assert.match(front, /pudge-pitch-mora low rise">え<\/span>/);
  assert.match(front, /pudge-pitch-mora high drop">ら<\/span>/);
  assert.match(front, /pudge-pitch-mora low">ぶ<\/span>/);
  assert.equal((front.replace(/<[^>]*>/g,'').match(/選ぶ/g) || []).length, 1);
  const plain = {...card(1), readings:[{readingIndex:0, text:'えらぶ'}]};
  assert(h.test.frontWordHtml(plain, false).includes('hide-reading'));
  const long = {...plain, wordTextPlain:'非常に長い日本語の単語'};
  assert.match(h.test.frontWordHtml(long, true), /sidebar-due-front-reading[^>]*><span class="pudge-inline-pitch/);
  assert(!h.test.answerDetails(card(1)).includes('pudge-inline-pitch'));
  const noPitch = {...plain, pitchAccents:[]};
  assert.match(h.test.frontWordHtml(noPitch,true), />えらぶ<\/span>/);
  const voiced = {paragraphs:['ｶﾞか\u3099次'],tokens:[]};
  assert.equal(h.test.paragraphPosition(voiced,{chapter_char_offset_exact:2.5}).local,4);
  const answer = h.test.answerDetails(card(1));
  assert(!answer.includes('pudge-pitch-mora'));
  assert(front.includes('pudge-pitch-mora'));
  assert(front.includes('>え</span>') && front.includes('>ら</span>') && front.includes('>ぶ</span>'));
  assert(!answer.includes('sidebar-due-reading') && !answer.includes('sidebar-due-answer-word'));
  const alternates = h.test.answerDetails({...card(1), readings:[
    ...card(1).readings, {readingIndex:1, text:'撰ぶ', rubyText:'撰[よ]ぶ'}]});
  assert.match(alternates, /sidebar-due-readings[^>]*>よぶ<\/div>/);
  assert(!alternates.includes('>えらぶ</div>'));
  h.test.set({previous:{card:card(1), reviewedAt:Date.now()}});
  const previous = h.test.previousReviewHtml();
  assert(previous.includes('data-sc-undo'));
  assert(!previous.includes('choose') && !previous.includes('Long source'));

  // The displayed reading opens Jiten on front, answer and previous cards.
  const urls = [];
  const links = harness({open_url:async url => { urls.push(url); }});
  const linkedCard = {...card(3), readingIndex:2};
  for (const revealed of [false,true]) {
    links.test.set({card:linkedCard, revealed});
    links.test.renderDue();
    assert.match(links.host.innerHTML,/href="https:\/\/jiten.moe\/vocabulary\/3\/2"/);
  }
  links.test.set({previous:{card:linkedCard,reviewedAt:Date.now()}});
  assert.match(links.test.previousReviewHtml(),/data-sc-jiten-reading="2"/);
  const link = {dataset:{scJitenWord:'3',scJitenReading:'2'}};
  let linkPrevented = 0;
  const click = {target:{closest:selector => selector==='[data-sc-jiten-word]' ? link : null},
    preventDefault(){linkPrevented++;},stopPropagation(){}};
  for(const callback of links.listeners.get('click')) callback(click);
  await flush();
  assert.equal(linkPrevented,1);
  assert.deepEqual(urls,['https://jiten.moe/vocabulary/3/2']);
  assert.equal(links.test.state().revealed,true,'opening Jiten does not grade or change reveal state');
  assert.doesNotMatch(links.test.frontWordHtml({...card(1),wordId:0}),/data-sc-jiten-word/);

  // Settings from the backend survive a different browser-origin store.
  const settings = harness({sidebar_review_settings:async () => ({interval:'600000'})});
  await settings.test.restoreSettings();
  assert.equal(settings.test.intervalValue(), '600000');
  const migrated = [];
  const legacy = harness({sidebar_review_settings:async value => {if(value)migrated.push(value);return {interval:''};}});
  await legacy.test.restoreSettings();
  assert.deepEqual(migrated, ['continuous']);

  // A stale stopped sample must not hide a just-starting player.
  const startup = harness();
  const starting = {id:8,playing:true,player_running:true};
  startup.test.acceptAudioState({books:[starting]},{optimistic:true});
  assert.equal(startup.test.reconcileAudioState({books:[]}).books[0].id,8);
  startup.test.acceptAudioState({books:[]},{rollback:true});
  assert.equal(startup.test.activeBook(),null);

  // Previous jumps to chapter start after five seconds, otherwise to the previous chapter.
  const seeks = [];
  const chapters = [{start:0,end:20,title:'One'},{start:20,end:40,title:'Two'},{start:40,end:60,title:'Three'}];
  const chapterApi = harness({audiobook_seek_to:async(id, at) => {seeks.push([id, at]);return {};}});
  chapterApi.test.set({audio:{books:[{id:4,playing:true,position:28,duration:60,chapters}]}});
  await chapterApi.test.audioClick('previous-chapter', {});
  chapterApi.test.set({audio:{books:[{id:4,playing:true,position:24,duration:60,chapters}]}});
  await chapterApi.test.audioClick('previous-chapter', {});
  await chapterApi.test.audioClick('next-chapter', {});
  assert.deepEqual(seeks, [[4,20],[4,0],[4,40]]);

  // Images and punctuation do not advance the normalized reading clock.
  const payload = {paragraphs:['[[PUDGE_LN_IMAGE_URL:test.png]]','甲乙 丙丁、戊己'],
    tokens:[[],[{start:6,end:8},{start:0,end:2},{start:3,end:5}]]};
  const position = h.test.paragraphPosition(payload, {chapter_char_offset_exact:2.5});
  assert.equal(position.index, 1);
  const windowed = h.test.liveWindow(payload, position);
  assert.equal(windowed.activeTokenIndex, 1);
  assert.equal(windowed.activeTokenProgress, 25);
  const edge = h.test.liveWindow({paragraphs:['あ'.repeat(120)], tokens:[[{start:6,end:45}]]},
    {index:0, local:30, localAudio:30});
  assert.equal(edge.cropStart, 0);
  assert.equal(edge.activeTokenIndex, 0);
  const unparsed = h.test.liveWindow({paragraphs:['あ'.repeat(200)]}, {index:0,local:120,localAudio:120});
  assert(unparsed.payload.paragraphs[0].length <= 540);
  assert(unparsed.activeTokenIndex >= 0);

  // Prefetched next card is visible while the provider is still saving.
  const save = deferred();
  let exclusions;
  const fast = harness({sidebar_due_review_submit:() => save.promise,
    sidebar_due_review_cards:async (_count, excluded) => { exclusions=excluded; return {cards:[card(1),card(2),card(3),card(4)]}; }});
  fast.test.set({card:card(1), queue:[card(2),card(3)], revealed:true});
  const submitted = fast.test.submitDue('good');
  assert.equal(fast.test.state().previous.card.wordId, 1);
  assert.equal(fast.test.state().previous.grade, 'good');
  assert.equal(fast.test.state().previous.pending, true);
  assert(fast.test.previousReviewHtml().includes('sidebar-due-last-grade'));
  assert.equal(fast.test.state().card.wordId, 2);
  await flush();
  assert(exclusions.includes('1:0'));
  assert(!fast.test.state().queue.some(row => row.wordId === 1));
  save.resolve({ok:true, outcome:'confirmed'});
  await submitted;
  assert.equal(fast.test.state().card.wordId, 2);
  assert(!fast.test.state().busy);

  // A rejected save restores the original answer and preserves the next card.
  const fail = deferred();
  const retry = harness({sidebar_due_review_submit:() => fail.promise,
    sidebar_due_review_cards:async () => ({cards:[]})});
  retry.test.set({card:card(1), queue:[card(2)], revealed:true});
  const failed = retry.test.submitDue('good');
  fail.resolve({ok:false, message:'offline'});
  await failed;
  assert.equal(retry.test.state().card.wordId, 1);
  assert.equal(retry.test.state().queue[0].wordId, 2);
  assert(retry.test.state().revealed);

  // Concurrent checks cannot consume two cards for a single display slot.
  const fetch = deferred();
  const concurrent = harness({sidebar_due_review_cards:() => fetch.promise});
  const a=concurrent.test.checkDue(), b=concurrent.test.checkDue();
  fetch.resolve({cards:[card(1),card(2)]});
  await Promise.all([a,b]);
  assert.equal(concurrent.test.state().card.wordId, 1);
  assert.equal(concurrent.test.state().queue[0].wordId, 2);

  // Invalidated requests cannot reintroduce a card reviewed in another UI.
  const old = deferred(); let calls=0;
  const external = harness({sidebar_due_review_cards:() => ++calls===1 ? old.promise : Promise.resolve({cards:[card(2)]})});
  const prefetched=external.context.PudgeSidebarCompanion.prefetch(true);
  external.test.noteExternalReview({detail:{backend:'jiten',wordId:1,readingIndex:0}});
  old.resolve({cards:[card(1)]});
  await prefetched; await flush();
  assert.deepEqual(Array.from(external.test.state().queue, row => row.wordId), [2]);

  // Hidden reviews leave Space/Undo to the audiobook and the main reader.
  h.test.set({card:card(1), revealed:false, audio:{books:[{id:5,player_running:true,playing:false}]}});
  let prevented=false;
  h.test.handleKeydown({key:' ',code:'Space',preventDefault:() => { prevented=true; }});
  assert(!prevented && !h.test.state().revealed);
  h.test.set({audio:{books:[{id:5,player_running:false,playing:false}]}});
  assert.equal(h.test.activeBook(), null);

  // Energy saving hides the review slot and leaves keyboard controls alone.
  let dueFetches = 0;
  const lowPower = harness({sidebar_due_review_cards:async()=>{dueFetches++;return {cards:[card(2)]};}});
  lowPower.context.document.documentElement.classList.contains = name => name === 'energy-saving';
  lowPower.test.set({card:card(1), revealed:false});
  await lowPower.test.checkDue();
  await lowPower.context.PudgeSidebarCompanion.prefetch(true);
  lowPower.test.renderDue();
  lowPower.test.handleKeydown({key:' ',code:'Space',preventDefault:()=>{},stopPropagation:()=>{}});
  assert.equal(dueFetches,1);assert.equal(lowPower.host.hidden,false);
  assert.equal(lowPower.test.state().revealed,false);
  assert(!lowPower.test.answerDetails({...card(1),frequencyRank:100,partsOfSpeech:['noun'],isLeech:true}).includes('sidebar-due-meta'));
  const delayed = deferred();
  let undos = 0;
  const undo = harness({sidebar_due_review_submit:()=>delayed.promise,
    sidebar_due_review_undo:async()=>{undos++;return {ok:true,outcome:'undone'};},
    sidebar_due_review_cards:async()=>({cards:[]})});
  undo.test.set({card:card(1),queue:[card(2)],revealed:true});
  const saving = undo.test.submitDue('hard');
  await undo.test.undoLastSidebarReview();
  assert.equal(undos, 0);
  delayed.resolve({ok:true,outcome:'confirmed'});
  await saving; await flush();
  assert.equal(undos, 1);
  assert.equal(undo.test.state().card.wordId, 1);

  console.log('sidebar companion behavioral scenarios: PASS');
})().catch(error => { console.error(error); process.exitCode=1; });
