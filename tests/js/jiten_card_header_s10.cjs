'use strict';
// Plan §10: Jiten card header contract. Headword column + ↗/★ controls,
// optional header actions and close are measured together; when they cannot
// share one row inside the card cap the header actions move to their own row.
// Geometry here is stubbed: this proves the sizing decision, not WebKit paint.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const WEB = process.argv[2];
const byId = new Map();
let narrow = false;
let sizes = {term: 100, actions: 0, close: 36};
const setProps = [];

function node(kind) {
  return {
    kind, dataset: {},
    get scrollWidth() { return 0; },
    getBoundingClientRect() {
      // Natural (single-line) width only while the card is in measuring mode.
      const measuring = pop.classList.contains('pudge-study-measuring');
      const natural = sizes[kind] || 0;
      return {left: 0, top: 0, right: 0, bottom: 0, height: 30, width: measuring ? natural : Math.min(natural, 120)};
    },
  };
}
const classes = new Set();
const head = node('head');
const termWrap = node('term');
const actions = node('actions');
const close = node('close');
const pop = {
  dataset: {}, innerHTML: '',
  classList: {add: (...x) => x.forEach(v => classes.add(v)), remove: (...x) => x.forEach(v => classes.delete(v)), contains: v => classes.has(v)},
  style: {setProperty: (k, v) => setProps.push([k, v]), removeProperty() {}},
  getBoundingClientRect: () => ({left: 0, top: 0, width: 440, height: 300}),
  querySelector(sel) {
    if (sel === '.pudge-study-head') return head;
    if (sel === '.pudge-study-term-wrap') return termWrap;
    if (sel === '.pudge-study-head-actions') return this.innerHTML.includes('class="pudge-study-head-actions"') ? actions : null;
    if (sel === '.pudge-study-close') return close;
    return null;
  },
  querySelectorAll: () => [],
};
byId.set('pudgeStudyCard', pop);
byId.set('pudgeTranslationPop', {classList: {add() {}, remove() {}, contains: () => false}, style: {}});
const document = {
  documentElement: {lang: 'en', style: {setProperty() {}}, dataset: {}},
  body: {appendChild() {}}, activeElement: null,
  createElement: () => ({style: {}, classList: {add() {}, remove() {}, contains: () => false}, dataset: {}}),
  getElementById: id => byId.get(id) || null,
  querySelector: () => null, querySelectorAll: () => [], addEventListener() {},
};
const window = {
  matchMedia: () => ({matches: narrow}), innerWidth: 1200, innerHeight: 900,
  addEventListener() {}, dispatchEvent() {}, toast() {},
  pywebview: {api: {study_decks: async () => []}},
};
const context = {
  window, document, console, Date, Math, JSON, Promise, Set, Map, Number, String, Array, Object, RegExp, Error,
  CustomEvent: function () {}, requestAnimationFrame: fn => fn(), setTimeout: () => 1, clearTimeout() {},
  getComputedStyle: () => ({columnGap: '12px', paddingLeft: '14px', paddingRight: '14px', borderLeftWidth: '1px', borderRightWidth: '1px'}),
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext('const ui={state:{settings:{}}};', context);
for (const name of ['review_actions.js', 'reading_tools.js']) {
  vm.runInContext(fs.readFileSync(path.join(WEB, name), 'utf8'), context, {filename: name});
}
const study = window.PudgeReadingTools.study;
const CHROME = 30; // 14 + 14 padding + 1 + 1 border

async function open(spec, withActions) {
  sizes = {...spec};
  setProps.length = 0;
  delete head.dataset.layout;
  study.close();
  const token = {wordId: 3, readingIndex: 0, surface: 'バニラ', fallback: true, card: {spelling: 'バニラ', reading: 'ばにら'}};
  const actionsList = withActions ? [
    {id: 'paired-audio-here', label: 'Play from here', placement: 'header', run() {}},
    {id: 'read-up-to-here', label: 'Read up to here', placement: 'header', run() {}},
  ] : [];
  await study.open({token, target: {getBoundingClientRect: () => ({left: 10, top: 10, right: 50, bottom: 30, width: 40, height: 20})}, backend: 'jiten', actions: actionsList});
  assert.ok(!classes.has('pudge-study-measuring'), 'measuring class is removed');
  const width = setProps.find(([k]) => k === '--pudge-study-width')?.[1] || null;
  return {layout: head.dataset.layout, width, html: pop.innerHTML};
}

(async () => {
  // Markup: ↗/★ live in their own controls slot; close is a separate header cell.
  let r = await open({term: 100, close: 36}, true);
  assert.match(r.html, /<div class="pudge-study-term-wrap"><div class="pudge-study-term">[\s\S]*?<\/div><div class="pudge-study-term-controls">/);
  assert.match(r.html, /class="pudge-study-head-actions"[\s\S]*Play from here[\s\S]*Read up to here[\s\S]*<\/div>\s*<button class="pudge-study-close"/);
  r = await open({term: 100, close: 36}, false);
  assert.doesNotMatch(r.html, /pudge-study-head-actions/, 'no empty actions cell');
  assert.equal(r.layout, 'compact');
  assert.equal(r.width, '440px');

  // Everything fits on one row: inline, width covers term+controls+actions+close+gaps.
  r = await open({term: 200, actions: 300, close: 36}, true);
  assert.equal(r.layout, 'inline');
  assert.equal(r.width, `${200 + 12 + 300 + 12 + 36 + CHROME}px`);

  // Too wide for the 620 cap: actions get their own row, no 620px overflow.
  r = await open({term: 300, actions: 300, close: 36}, true);
  assert.equal(r.layout, 'stacked');
  assert.equal(r.width, '440px');
  r = await open({term: 560, actions: 300, close: 36}, true);
  assert.equal(r.layout, 'stacked');
  assert.equal(r.width, '620px', 'long headword uses the cap and wraps');

  // A very long headword without actions: capped, the term column wraps.
  r = await open({term: 900, close: 36}, false);
  assert.equal(r.layout, 'compact');
  assert.equal(r.width, '620px');

  // Viewport cap below 620 also triggers stacking.
  window.innerWidth = 600;
  r = await open({term: 200, actions: 300, close: 36}, true);
  assert.equal(r.layout, 'stacked');
  window.innerWidth = 1200;

  // Narrow (<=520px) viewport: stacked, width left to CSS.
  narrow = true;
  r = await open({term: 200, actions: 300, close: 36}, true);
  assert.equal(r.layout, 'stacked');
  assert.equal(r.width, null);
  narrow = false;

  // CSS contract: real column bounds, headword wraps, no clipping.
  const css = fs.readFileSync(path.join(WEB, 'reading_tools.css'), 'utf8');
  const rule = sel => {
    const m = new RegExp(`(?:^|[}\\n])${sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\{([^}]*)\\}`).exec(css);
    assert.ok(m, `missing rule ${sel}`);
    return m[1];
  };
  assert.match(rule('.pudge-study-head'), /display:grid;grid-template-columns:minmax\(0,1fr\) auto auto/);
  assert.match(rule('.pudge-study-head[data-layout="stacked"]'), /"term close" "actions actions"/);
  const term = rule('.pudge-study-term');
  assert.match(term, /white-space:normal/);
  assert.match(term, /min-width:0/);
  assert.doesNotMatch(term, /nowrap|keep-all|overflow:hidden|text-overflow/);
  assert.match(rule('.pudge-study-term-controls'), /flex:0 0 auto/);
  assert.match(rule('.pudge-study-close'), /grid-area:close/);
  assert.match(rule('.pudge-study-head-actions'), /grid-area:actions/);
  console.log('jiten card header S10: PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
