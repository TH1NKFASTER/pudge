'use strict';
// Plan §5: a validated Jiten live-state response must reach the OPEN card even
// when it is not a registered [data-pudge-study-token] (LN uses its own token
// map), and a cached `studyDeckIds: []` alone must not block grading.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const WEB = process.argv[2];
const timeout = setTimeout(() => { console.error('jiten live card S5 timed out'); process.exit(1); }, 5000);

// ---- minimal DOM ------------------------------------------------------------
const byId = new Map();
const docListeners = {};
let visibleNodes = [];
function el() {
  const classes = new Set();
  return {
    dataset: {}, style: {setProperty() {}, removeProperty() {}},
    classList: {add: (...x) => x.forEach(v => classes.add(v)), remove: (...x) => x.forEach(v => classes.delete(v)),
      contains: v => classes.has(v), [Symbol.iterator]: () => classes.values()},
    set id(v) { this._id = v; byId.set(v, this); }, get id() { return this._id; },
    set className(v) { String(v).split(/\s+/).filter(Boolean).forEach(x => classes.add(x)); },
    innerHTML: '', getBoundingClientRect: () => ({left: 0, top: 0, right: 40, bottom: 10, width: 40, height: 10}),
    querySelector: () => null, querySelectorAll: () => [], contains: () => false, closest: () => null,
  };
}
const document = {
  documentElement: {lang: 'en', style: {setProperty() {}}, dataset: {}},
  body: {appendChild() {}},
  activeElement: null,
  createElement: () => el(),
  getElementById: id => byId.get(id) || null,
  querySelector: () => null,
  querySelectorAll: sel => (sel === '[data-pudge-study-token]' ? visibleNodes : []),
  addEventListener: (type, fn) => { (docListeners[type] ||= []).push(fn); },
};

// ---- controllable bridge ------------------------------------------------------
let account = 'jiten:account-A';
let liveDeckIds = [123];
let liveStatesOverride = null; // e.g. ['due'] for an SRS word outside every deck
let holdLive = null;            // when set, study_states waits on this promise
const liveCalls = [];
const submitted = [];
const toasts = [];
let actionResult = {ok: true, outcome: 'confirmed'};
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; }
const window = {
  matchMedia: () => ({matches: false}), innerWidth: 1200, innerHeight: 900,
  addEventListener() {}, dispatchEvent() {},
  toast: message => toasts.push(String(message)),
  pywebview: {api: {
    study_decks: async () => [{id: 5, name: 'Mining'}],
    study_provider_capabilities: async () => ({configured: true, account_key: account}),
    study_states: async payload => {
      liveCalls.push(JSON.parse(JSON.stringify(payload)));
      const scope = account;
      const ids = [...liveDeckIds];
      if (holdLive) await holdLive.promise;
      return {ok: true, provider: 'jiten', account_scope: scope, states: payload.words.map(([w, r]) => ({
        ok: true, provider: 'jiten', wordId: w, readingIndex: r, states: liveStatesOverride || (ids.length ? ['young'] : ['new']),
        normalizedState: liveStatesOverride ? '' : (ids.length ? 'learning' : 'new'), studyDeckIds: ids, stale: false,
      }))};
    },
    study_action: async payload => { submitted.push(payload); return actionResult; },
  }},
};
const context = {
  window, document, console, Date, Math, JSON, Promise, Set, Map, Number, String, Array, Object, RegExp, Error, Symbol,
  CustomEvent: function CustomEvent(type, init) { this.type = type; this.detail = init?.detail; },
  requestAnimationFrame: fn => fn(), getComputedStyle: () => ({}),
  setTimeout: () => 1, clearTimeout() {}, crypto: {randomUUID: () => `uuid-${submitted.length + 1}`},
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext('const ui={state:{settings:{}}};', context);
for (const name of ['review_actions.js', 'reading_tools.js']) {
  vm.runInContext(fs.readFileSync(path.join(WEB, name), 'utf8'), context, {filename: name});
}
const study = window.PudgeReadingTools.study;
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

function lnToken(wordId = 77, cached = []) {
  // Shape of an LN token passed straight to study.open from ui.lnTokenMap.
  return {wordId, readingIndex: 0, surface: '猫', idNamespace: 'jiten', reviewable: true,
    card: {spelling: '猫', reading: 'ねこ', states: ['new'], studyDeckIds: cached}};
}
function gradeButton(id) {
  const button = {dataset: {pudgeStudyReview: id}, disabled: false,
    closest: sel => (sel === '[data-pudge-study-review]' ? button : null)};
  return button;
}
async function click(target) {
  await Promise.all((docListeners.click || []).map(fn => fn({target, preventDefault() {}, stopPropagation() {}})));
}
function setDeck(value) {
  if (value === null) { byId.delete('pudgeStudyDeck'); return; }
  byId.set('pudgeStudyDeck', {value, dataset: {}, options: [], innerHTML: ''});
}
function reset() {
  study.close();
  liveCalls.length = 0; submitted.length = 0; toasts.length = 0;
  holdLive = null; liveDeckIds = [123]; liveStatesOverride = null; account = 'jiten:account-A';
  actionResult = {ok: true, outcome: 'confirmed'}; visibleNodes = []; setDeck(null);
}
async function establishScope() {
  await study.lookupLiveStates([[1, 0]]);
  assert.equal(study.currentAccountScope(), 'jiten:account-A');
  liveCalls.length = 0;
}

(async () => {
  await establishScope();

  // 1. Reproduction: scope set, unregistered LN token cached [], live [123], Good.
  reset();
  let token = lnToken();
  await study.open({token, target: el(), backend: 'jiten', sentence: '猫が好き'});
  await flush();
  assert.equal(liveCalls.length, 1, 'one live lookup on open');
  assert.deepEqual([...token.card.studyDeckIds], [123], 'unregistered active card receives live memberships');
  await click(gradeButton('good'));
  await flush();
  assert.equal(submitted.length, 1, 'exactly one review POST');
  assert.equal(submitted[0].grade, 'good');
  assert.equal(submitted[0].action, 'review');
  assert.equal(submitted[0].deck_id, '');
  assert.deepEqual(toasts, [], 'no deck toast');

  // 2. Every Jiten native grade sends its own id.
  for (const grade of ['again', 'hard', 'good', 'easy']) {
    reset();
    token = lnToken(80);
    await study.open({token, target: el(), backend: 'jiten'});
    await flush();
    await click(gradeButton(grade));
    await flush();
    assert.equal(submitted.length, 1, `${grade}: one POST`);
    assert.equal(submitted[0].grade, grade);
    assert.equal(submitted[0].id_namespace, 'jiten');
  }

  // 3. Grade before the live response: awaits the SAME request, double click is
  // ignored, one POST, no toast.
  reset();
  holdLive = deferred();
  token = lnToken(81);
  await study.open({token, target: el(), backend: 'jiten'});
  await flush();
  assert.equal(liveCalls.length, 1);
  const first = click(gradeButton('good'));
  const second = click(gradeButton('good'));
  await flush();
  assert.equal(submitted.length, 0, 'waits for live membership before deciding');
  assert.equal(liveCalls.length, 1, 'no duplicate live lookup while waiting');
  holdLive.resolve();
  await Promise.all([first, second]);
  await flush();
  assert.equal(liveCalls.filter(call => !call.force).length, 1, 'still the one open-time lookup');
  assert.equal(liveCalls.filter(call => call.force).length, 1, 'only the post-mutation confirm refresh follows');
  assert.equal(submitted.length, 1, 'double click → exactly one POST');
  assert.deepEqual(toasts, []);

  // 4. Truly new word: confirmed empty live memberships, no deck → toast, no POST.
  reset();
  liveDeckIds = [];
  token = lnToken(82);
  await study.open({token, target: el(), backend: 'jiten'});
  await flush();
  await click(gradeButton('good'));
  await flush();
  assert.equal(submitted.length, 0);
  assert.deepEqual(toasts, ['New word: choose a deck to add it to']);
  // ... same word with a selected deck → existing mining path, one POST with deck.
  toasts.length = 0;
  setDeck('5');
  await click(gradeButton('good'));
  await flush();
  assert.equal(submitted.length, 1);
  assert.equal(submitted[0].deck_id, '5');
  assert.deepEqual(toasts, []);

  // 5. Pending + truly new: waits, then requires a deck (card stays open for retry).
  reset();
  liveDeckIds = [];
  holdLive = deferred();
  token = lnToken(83);
  await study.open({token, target: el(), backend: 'jiten'});
  await flush();
  const waiting = click(gradeButton('easy'));
  await flush();
  holdLive.resolve();
  await waiting; await flush();
  assert.equal(submitted.length, 0);
  assert.deepEqual(toasts, ['New word: choose a deck to add it to']);
  assert.ok(study.isOpen(), 'card stays open after the deck requirement');
  setDeck('5');
  await click(gradeButton('easy'));
  await flush();
  assert.equal(submitted.length, 1, 'retry after choosing a deck is not blocked by the earlier wait');

  // 6. Card switched while the refresh is pending: late response cannot update
  // card B, and the pending grade on A is dropped (no POST).
  reset();
  holdLive = deferred();
  const tokenA = lnToken(84);
  await study.open({token: tokenA, target: el(), backend: 'jiten'});
  await flush();
  const pendingA = click(gradeButton('good'));
  await flush();
  const holdA = holdLive;
  holdLive = null;
  liveDeckIds = [];
  const tokenB = lnToken(85);
  await study.open({token: tokenB, target: el(), backend: 'jiten'});
  await flush();
  holdA.resolve();
  await pendingA; await flush();
  assert.equal(submitted.length, 0, 'grade on a closed/switched card is dropped');
  assert.deepEqual([...tokenB.card.studyDeckIds], [], 'card B keeps its own live result');

  // 7. Account switched during the refresh: stale-scope row is not applied.
  reset();
  holdLive = deferred();
  token = lnToken(86);
  await study.open({token, target: el(), backend: 'jiten'});
  await flush();
  account = 'jiten:account-B';      // capabilities now report a different account
  const switched = click(gradeButton('good'));
  holdLive.resolve();
  await switched; await flush();
  // The response was produced for account-A (scope captured before the switch).
  assert.deepEqual([...token.card.studyDeckIds], [], 'old-account memberships are not applied');
  assert.equal(submitted.length, 0);
  assert.deepEqual(toasts, ['New word: choose a deck to add it to']);
  account = 'jiten:account-A';      // restore account-A scope for following cases
  await establishScope();

  // 8. Registered visible token + the same open card are both updated.
  reset();
  const html = study.renderParsedText({paragraphs: ['猫'], tokens: [[{start: 0, end: 1, wordId: 90, readingIndex: 0}]],
    vocabulary: [{wordId: 90, readingIndex: 0, spelling: '猫', states: ['new'], studyDeckIds: []}]}, {backend: 'jiten'});
  const id = /data-pudge-study-token="([^"]+)"/.exec(html)[1];
  const node = el();
  node.dataset.pudgeStudyToken = id;
  visibleNodes = [node];
  assert.equal(await study.openElement(node), true);
  await flush();
  assert.equal(node.dataset.pudgeStudyVerified, '1', 'registered node verified');
  assert.ok(node.classList.contains('state-learning'), 'registered node recolored');
  await click(gradeButton('good'));
  await flush();
  assert.equal(submitted.length, 1);
  assert.deepEqual(toasts, []);

  // 9. Pending mutation is not overwritten by a refresh of the same pair.
  reset();
  holdLive = deferred();
  actionResult = {ok: true, outcome: 'unknown', message: 'unknown'};
  token = lnToken(91, [7]);         // already in a deck: grade posts immediately
  await study.open({token, target: el(), backend: 'jiten'});
  await flush();
  liveDeckIds = [];
  await click(gradeButton('good'));
  await flush();
  assert.equal(submitted.length, 1, 'existing membership: no wait');
  holdLive.resolve();
  await flush();
  assert.deepEqual([...token.card.studyDeckIds], [7], 'in-flight refresh does not overwrite a pending mutation');
  // outcome unknown → no automatic second POST.
  await flush();
  assert.equal(submitted.length, 1, 'outcome unknown is not retried');
  assert.deepEqual(toasts, ['unknown']);

  // 10. Live refresh unavailable (bridge failure) → cached [] still requires a deck.
  reset();
  window.pywebview.api.study_states = async () => { throw new Error('offline'); };
  token = lnToken(92);
  await study.open({token, target: el(), backend: 'jiten'});
  await flush();
  await click(gradeButton('good'));
  await flush();
  assert.equal(submitted.length, 0);
  assert.deepEqual(toasts, ['New word: choose a deck to add it to']);

  // 11. Legacy single-pair endpoint (the originally reproduced path): same
  // account scope, live [123] → the unregistered open card is updated too.
  reset();
  const savedStudyStates = window.pywebview.api.study_states;
  delete window.pywebview.api.study_states;
  const singleCalls = [];
  window.pywebview.api.study_state = async payload => {
    singleCalls.push(payload);
    return {ok: true, provider: 'jiten', wordId: payload.word_id, readingIndex: payload.reading_index,
      states: ['young'], normalizedState: 'learning', studyDeckIds: [123], stale: false, account_scope: 'jiten:account-A'};
  };
  token = lnToken(93);
  await study.open({token, target: el(), backend: 'jiten'});
  await flush();
  assert.equal(singleCalls.length, 1);
  assert.deepEqual([...token.card.studyDeckIds], [123]);
  await click(gradeButton('good'));
  await flush();
  assert.equal(submitted.length, 1);
  assert.deepEqual(toasts, []);

  // 12. Due/Mature word that is in Jiten SRS but in no study deck: reviews are
  // shared across decks, so grading needs no deck selection.
  for (const states of [['due'], ['mature']]) {
    reset();
    window.pywebview.api.study_states = savedStudyStates;
    liveDeckIds = [];
    liveStatesOverride = states;
    token = lnToken(94);
    token.card.states = states;
    await study.open({token, target: el(), backend: 'jiten'});
    await flush();
    await click(gradeButton('good'));
    await flush();
    assert.equal(submitted.length, 1, `${states[0]} word reviews without a deck`);
    assert.equal(submitted[0].deck_id, '');
    assert.deepEqual(toasts, []);
  }

  console.log('jiten live card S5: PASS');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => clearTimeout(timeout));
