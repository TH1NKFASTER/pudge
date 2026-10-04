'use strict';
// S3: jpdb card state is refreshed live (lookup-vocabulary) instead of trusting
// the parse-cache snapshot: hydration on render, account isolation, and no
// jpdb request for Jiten-ID fallback tokens.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const nodes = [];
const timers = [];
const calls = [];
let account = 'jpdb:acc-1';
let responseScope = 'jpdb:acc-1';
const document = {
  documentElement: {lang: 'en', style: {setProperty() {}}},
  body: {appendChild() {}},
  addEventListener() {},
  createElement() { return {style: {}, classList: {add() {}, remove() {}, contains() { return false; }}, set innerHTML(v) { this._html = v; }, get innerHTML() { return this._html || ''; }}; },
  getElementById() { return null; },
  querySelector() { return null; },
  querySelectorAll(sel) { return sel === '[data-pudge-study-token]' ? nodes : []; },
};
const window = {
  matchMedia() { return {matches: true}; }, innerWidth: 1000, innerHeight: 800,
  pywebview: {api: {
    study_states: async payload => {
      calls.push(JSON.parse(JSON.stringify(payload)));
      return {ok: true, provider: payload.backend, account_scope: responseScope,
        states: payload.words.map(([vid, sid]) => ({ok: true, wordId: vid, readingIndex: sid, provider: 'jpdb', states: ['known'], normalizedState: 'known', stale: false}))};
    },
    study_provider_capabilities: async backend => ({configured: true, account_key: backend === 'jpdb' ? account : 'jiten:x'}),
  }},
  addEventListener() {}, dispatchEvent() {},
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {
  window, document, CustomEvent: function () {}, requestAnimationFrame() {}, getComputedStyle() { return {}; },
  setTimeout(fn) { timers.push(fn); return timers.length; }, clearTimeout() {}, console, Date,
}, {filename: 'reading_tools.js'});
const study = window.PudgeReadingTools.study;

function mount(html) {
  for (const match of html.matchAll(/class="([^"]*)" data-pudge-study-token="([^"]+)"/g)) {
    const classes = new Set(match[1].split(' '));
    nodes.push({dataset: {pudgeStudyToken: match[2]}, classList: {
      _s: classes, add(v) { classes.add(v); }, remove(v) { classes.delete(v); }, contains(v) { return classes.has(v); },
      [Symbol.iterator]() { return classes[Symbol.iterator](); },
    }});
  }
}
async function runTimers() { while (timers.length) { await timers.shift()(); await new Promise(r => setImmediate(r)); } }

(async () => {
  // 1. Batch lookup goes to jpdb with native IDs and is account-scoped.
  const rows = await study.lookupLiveStates([[5, 1], [5, 1], [0, 1]], {backend: 'jpdb'});
  assert.deepEqual(calls.at(-1), {backend: 'jpdb', words: [[5, 1]]});
  assert.equal(rows.length, 1);
  assert.equal(rows[0].provider, 'jpdb');
  assert.equal(rows[0].account_scope, 'jpdb:acc-1');

  // 2. Response for another jpdb account (token switched mid-call) is dropped.
  responseScope = 'jpdb:old';
  assert.equal((await study.lookupLiveStates([[5, 1]], {backend: 'jpdb'})).length, 0);
  responseScope = 'jpdb:acc-1';

  // 3. Rendering a cached jpdb parse (snapshot says "new") hydrates live state;
  //    the Jiten-ID fallback token in the same payload is not sent to jpdb.
  calls.length = 0;
  const payload = {
    paragraphs: ['猫が好き'],
    tokens: [[
      {start: 0, end: 1, wordId: 7, readingIndex: 0, idNamespace: 'jpdb'},
      {start: 2, end: 4, wordId: 9001, readingIndex: 0, idNamespace: 'jiten'},
    ]],
    vocabulary: [
      {wordId: 7, readingIndex: 0, idNamespace: 'jpdb', cardState: ['new'], states: ['new'], inDeck: true},
      {wordId: 9001, readingIndex: 0, idNamespace: 'jiten', states: []},
    ],
  };
  mount(study.renderParsedText(payload, {backend: 'jpdb'}));
  assert.ok(nodes[0].classList.contains('state-new'), 'snapshot state first');
  await runTimers();
  assert.deepEqual(calls, [{backend: 'jpdb', words: [[7, 0]]}], 'only native jpdb IDs are looked up');
  assert.ok(nodes[0].classList.contains('state-known'), 'live jpdb state applied');
  assert.ok(!nodes[0].classList.contains('state-new'));
  assert.equal(nodes[0].dataset.pudgeStudyVerified, '1');
  assert.ok(!nodes[1].classList.contains('state-known'), 'Jiten-ID fallback token untouched');

  // 4. Jiten rendering keeps using the Jiten backend.
  calls.length = 0;
  study.renderParsedText({paragraphs: ['犬'], tokens: [[{start: 0, end: 1, wordId: 3, readingIndex: 0}]], vocabulary: [{wordId: 3, readingIndex: 0}]}, {backend: 'jiten'});
  await runTimers();
  assert.equal(calls[0]?.backend, 'jiten');
  console.log('jpdb live state S3: PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
