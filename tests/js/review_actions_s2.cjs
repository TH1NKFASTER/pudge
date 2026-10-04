'use strict';
const lifecycleTimeout=setTimeout(()=>{console.error('Review lifecycle test timed out');process.exit(1);},5000);
// S2 behaviour: provider x mode grade buttons (4/5/2), shortcut matching,
// hidden-profile bindings, Space confirm, IME/input/repeat guards, and the
// review gate rendering its active Jiten action set. Minimal fake DOM, no deps.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const WEB = process.argv[2];

// ---- tiny fake DOM ---------------------------------------------------------
const registry = new Map();
let activeElement = null;

function parseAttrs(raw) {
  const attrs = {};
  for (const m of raw.matchAll(/([a-zA-Z_:-][\w:.-]*)(?:="([^"]*)")?/g)) attrs[m[1]] = m[2] ?? '';
  return attrs;
}

class El {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this._html = '';
    this._cache = new Map();
    this.dataset = {};
    this.style = {setProperty() {}, removeProperty() {}};
    this.listeners = {};
    this.attrs = {};
    this.parent = null;
    this.isConnected = true;
    const set = new Set();
    this.classList = {
      add: (...xs) => xs.forEach(x => set.add(x)),
      remove: (...xs) => xs.forEach(x => set.delete(x)),
      toggle: (x, on) => { const v = on === undefined ? !set.has(x) : on; v ? set.add(x) : set.delete(x); return v; },
      contains: x => set.has(x),
    };
  }
  set id(v) { this._id = v; registry.set(v, this); }
  get id() { return this._id; }
  set className(v) { String(v).split(/\s+/).filter(Boolean).forEach(x => this.classList.add(x)); }
  set innerHTML(html) { this._html = String(html); this._cache.clear(); }
  get innerHTML() { return this._html; }
  get textContent() { return this._html.replace(/<[^>]+>/g, ''); }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k]; }
  appendChild(child) { child.parent = this; return child; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener() {}
  contains() { return true; }
  focus() { activeElement = this; }
  blur() { if (activeElement === this) activeElement = null; }
  getBoundingClientRect() { return {left: 0, top: 0, right: 40, bottom: 10, width: 440, height: 300}; }
  closest(selector) {
    const m = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(selector);
    if (m && this.attrs[m[1]] !== undefined && (m[2] === undefined || this.attrs[m[1]] === m[2])) return this;
    if (this.parent && selector === '#pudgeStudyCard' && this.parent.id === 'pudgeStudyCard') return this.parent;
    return null;
  }
  click() {
    let node = this;
    while (node) {
      for (const fn of node.listeners.click || []) fn({target: this, preventDefault() {}, stopPropagation() {}});
      node = node.parent;
    }
    for (const fn of documentListeners.click || []) fn({target: this, preventDefault() {}, stopPropagation() {}});
  }
  _elements(selector) {
    if (this._cache.has(selector)) return this._cache.get(selector);
    const out = [];
    const cls = /^\.([\w-]+)$/.exec(selector);
    const attr = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(selector);
    for (const m of this._html.matchAll(/<(\w+)((?:\s+[^>]*)?)>/g)) {
      const attrs = parseAttrs(m[2] || '');
      const classes = String(attrs.class || '').split(/\s+/);
      const hit = cls ? classes.includes(cls[1])
        : attr ? attrs[attr[1]] !== undefined && (attr[2] === undefined || attrs[attr[1]] === attr[2])
        : false;
      if (!hit) continue;
      const el = new El(m[1]);
      el.attrs = attrs;
      el.parent = this;
      el.disabled = attrs.disabled !== undefined;
      for (const [k, v] of Object.entries(attrs)) {
        if (k.startsWith('data-')) el.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = v;
      }
      // Nested card root keeps its own innerHTML for later queries.
      if (cls && cls[1] === 'pudge-review-gate-card') {
        if (!this._gateCard) this._gateCard = el;
        out.push(this._gateCard);
        continue;
      }
      out.push(el);
    }
    this._cache.set(selector, out);
    return out;
  }
  querySelector(selector) {
    if (this._gateCard && selector !== '.pudge-review-gate-card') {
      const inner = this._gateCard.querySelector(selector);
      if (inner) return inner;
    }
    return this._elements(selector)[0] || null;
  }
  querySelectorAll(selector) { return this._elements(selector); }
}

const documentListeners = {};
const document = {
  documentElement: new El('html'),
  body: new El('body'),
  get activeElement() { return activeElement; },
  createElement: tag => new El(tag),
  getElementById: id => registry.get(id) || null,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: (type, fn) => { (documentListeners[type] ||= []).push(fn); },
  createRange: () => ({selectNodeContents() {}, setEnd() {}, cloneContents: () => new El()}),
};
document.documentElement.lang = 'en';

const submitted = [];
const window = {
  matchMedia: () => ({matches: true}),
  innerWidth: 1200, innerHeight: 900,
  addEventListener() {},
  toast() {},
  pywebview: {api: {
    study_decks: async () => [],
    study_state: async () => ({ok: true, states: []}),
    study_action: async payload => { submitted.push(payload); return {ok: true, outcome: 'confirmed'}; },
    review_gate_ui_event: async () => ({}),
    review_gate_begin: async () => gateStatus,
    review_gate_review: async (...args) => { submitted.push({gate: args}); return {ok: true, outcome: 'confirmed', completed: 1, remaining: 0}; },
  }},
};
const context = {
  window, document, globalThis: null, console, Date, Math, JSON, Promise, Set, Map, Number, String, Array, Object, RegExp, Error,
  requestAnimationFrame: fn => fn(), cancelAnimationFrame() {}, getComputedStyle: () => ({}),
  setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {},
  performance: {now: () => 0}, crypto: {randomUUID: () => 'uuid-1'},
};
context.globalThis = context;
Object.assign(window, {document});
vm.createContext(context);
vm.runInContext('const ui={state:{settings:{}}};',context);
for (const name of ['review_actions.js', 'reading_tools.js', 'review_gate.js']) {
  vm.runInContext(fs.readFileSync(path.join(WEB, name), 'utf8'), context, {filename: name});
}
const RA = context.PudgeReviewActions;
const study = context.window.PudgeReadingTools?.study || context.PudgeReadingTools?.study;
const gate = context.window.PudgeReviewGate || context.PudgeReviewGate;
assert.ok(RA && study && gate, 'modules loaded');

function key(k, code, extra = {}) {
  return {key: k, code, isComposing: false, repeat: false, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false,
    target: {closest: () => null}, preventDefault() { this.prevented = true; }, stopPropagation() {}, stopImmediatePropagation() {}, ...extra};
}

// ---- 1. matching ----------------------------------------------------------
const jpdbNative = RA.defaultSet('jpdb', 'native');
assert.equal(RA.matchAction(key('5', 'Digit5'), jpdbNative)?.id, 'easy');
assert.equal(RA.matchAction(key('5', 'Numpad5'), jpdbNative)?.id, 'easy', 'numpad');
assert.equal(RA.matchAction(key('&', 'Digit1', {shiftKey: true}), jpdbNative), null, 'shift not bound');
assert.equal(RA.matchAction(key('1', 'Digit1', {repeat: true}), jpdbNative), null, 'repeat ignored');
assert.equal(RA.matchAction(key('1', 'Digit1', {isComposing: true}), jpdbNative), null, 'IME ignored');
assert.equal(RA.matchAction(key('1', 'Digit1', {target: {closest: () => ({})}}), jpdbNative), null, 'text input ignored');
const custom = {actions: [{id: 'fail', shortcut: 'f'}, {id: 'pass', shortcut: 'Ctrl+j'}, {id: 'off', shortcut: ''}]};
assert.equal(RA.matchAction(key('а', 'KeyF'), custom)?.id, 'fail', 'Russian layout uses physical key');
assert.equal(RA.matchAction(key('j', 'KeyJ'), custom), null, 'modifier required');
assert.equal(RA.matchAction(key('j', 'KeyJ', {ctrlKey: true}), custom)?.id, 'pass');
assert.equal(RA.matchAction(key('3', 'Digit3'), RA.defaultSet('jiten', 'binary')), null, 'hidden native key does not fire in binary');
assert.equal(JSON.stringify(RA.conflicts({actions: [{id: 'a', shortcut: '1'}, {id: 'b', shortcut: '1'}]})), JSON.stringify([['a', 'b', '1']]));

// ---- 2. study popup renders exactly the active set ------------------------
function settingsFor(mode, overrides = {}) {
  const sets = {};
  for (const [p, m] of [['jiten', 'native'], ['jiten', 'binary'], ['jpdb', 'native'], ['jpdb', 'binary']]) {
    const set = RA.defaultSet(p, m);
    for (const action of set.actions) {
      const k = `${p}:${m}:${action.id}`;
      if (k in overrides) action.shortcut = overrides[k];
    }
    sets[`${p}:${m}`] = set;
  }
  return {review_mode: mode, review_action_sets: sets};
}

async function openCard(backend) {
  const token = {wordId: 7, readingIndex: 8, surface: '猫', idNamespace: backend, reviewable: true,
    card: {spelling: '猫', reading: 'ねこ', states: ['known'], idNamespace: backend}};
  const target = new El('span');
  await study.open({token, target, backend, sentence: '猫が好き'});
  const pop = document.getElementById('pudgeStudyCard');
  assert.ok(pop?.classList.contains('open'), 'popup open');
  return pop;
}

function gradeIds(html) {
  return [...html.matchAll(/data-pudge-study-review="([^"]+)"/g)].map(m => m[1]);
}

(async () => {
  RA.update(settingsFor('native'));
  let pop = await openCard('jpdb');
  assert.deepEqual(gradeIds(pop.innerHTML), ['nothing', 'something', 'hard', 'okay', 'easy']);
  assert.match(pop.innerHTML, /data-count="5"/);
  assert.match(pop.innerHTML, /grade-something/);

  // Key 2 selects Something; Space confirms and sends the native jpdb action.
  const ev = key('2', 'Digit2');
  assert.equal(study.handleReviewKeydown(ev), true);
  assert.equal(activeElement?.dataset?.pudgeStudyReview, 'something');
  assert.equal(submitted.length, 0, 'digit alone never submits');
  const space = key(' ', 'Space');
  assert.equal(study.handleReviewKeydown(space), true);
  await Promise.resolve(); await Promise.resolve();
  assert.equal(submitted.length, 1);
  assert.equal(submitted[0].grade, 'something');
  assert.equal(submitted[0].id_namespace, 'jpdb');
  submitted.length = 0;
  study.close?.();

  RA.update(settingsFor('binary'));
  pop = await openCard('jpdb');
  assert.deepEqual(gradeIds(pop.innerHTML), ['fail', 'pass']);
  assert.equal(study.handleReviewKeydown(key('5', 'Digit5')), false, 'jpdb native key hidden in binary');
  study.close?.();

  pop = await openCard('jiten');
  assert.deepEqual(gradeIds(pop.innerHTML), ['fail', 'pass']);
  study.close?.();

  RA.update(settingsFor('native', {'jiten:native:again': 'q', 'jiten:native:hard': ''}));
  pop = await openCard('jiten');
  assert.deepEqual(gradeIds(pop.innerHTML), ['again', 'hard', 'good', 'easy']);
  assert.equal(study.handleReviewKeydown(key('1', 'Digit1')), false, 'rebound default no longer fires');
  assert.equal(study.handleReviewKeydown(key('2', 'Digit2')), false, 'disabled binding');
  assert.equal(study.handleReviewKeydown(key('q', 'KeyQ')), true);
  assert.equal(activeElement?.dataset?.pudgeStudyReview, 'again');
  study.close?.();

  // A stale/foreign action injected into the DOM is ignored by the click path.
  // ---- 3. review gate renders the server-provided Jiten set ---------------
  gateStatus = {
    blocking: true, granted: false, required: 1, completed: 0, remaining: 1,
    cards: [{wordId: 1, readingIndex: 0, pudgeCardKey: '1:0', wordText: '猫', readings: [{text: 'ねこ'}],
      intervalPreview: {againSeconds: 60, goodSeconds: 86400}}],
    review_actions: RA.defaultSet('jiten', 'binary'),
  };
  await gate.open({path: '/v.mkv'}, null);
  const overlay = document.getElementById('pudgeReviewGate');
  assert.ok(overlay?.classList.contains('open'), 'gate open');
  assert.equal(gate.showAnswer(), true);
  const cardHtml = overlay.querySelector('.pudge-review-gate-card').innerHTML;
  assert.deepEqual([...cardHtml.matchAll(/data-review-gate-grade="([^"]+)"/g)].map(m => m[1]), ['fail', 'pass']);
  assert.match(cardHtml, /Fail<small>1m<\/small>/, 'Fail shows the again interval');
  assert.match(cardHtml, /Pass<small>1d<\/small>/, 'Pass shows the good interval');
  assert.equal(gate.handleKeydown(key('3', 'Digit3')), false, 'native Jiten key hidden in binary gate');
  assert.equal(gate.handleKeydown(key('2', 'Digit2')), true);
  assert.equal(activeElement?.dataset?.reviewGateGrade, 'pass');
  gate.close();

  // The real app uses a global lexical const, not window.ui. Disabled gates
  // never create an overlay or contact the provider, including chapter changes.
  vm.runInContext('Object.assign(ui.state.settings,{review_gate_ln_enabled:false,review_gate_manga_enabled:false});',context);
  assert.equal(window.ui,undefined);
  window.pywebview.api.content_review_begin=async()=>{throw new Error('disabled content gate called the provider');};
  for(const kind of ['ln','manga'])for(const part of [0,1,20,40]) {
    assert.equal(await gate.require(kind,4,part),true);
    assert.equal(gate.isOpen(),false);
  }
  vm.runInContext('ui.state.settings.review_gate_ln_enabled=true;',context);

  // Cancelling a content gate while the provider is loading rejects entry.
  const flush = async()=>{for(let i=0;i<10;i++)await Promise.resolve();};
  let completeFetch;
  window.pywebview.api.content_review_begin=()=>new Promise(resolve=>{completeFetch=resolve;});
  let entryResult;
  const entry=gate.require('ln',4,0).then(result=>{entryResult=result;});
  await flush();
  assert.equal(entryResult,undefined);
  assert.ok(gate.isOpen());
  overlay.click();
  await entry;
  assert.equal(entryResult,false);
  completeFetch({granted:true,blocking:false,checked:true,reason:'no_due_cards',required:0,cards:[]});
  await flush();
  assert.equal(gate.isOpen(),false,'late provider reply cannot reopen or enter content');

  // A verified empty chapter remains on its gate until the user continues.
  window.pywebview.api.content_review_begin=async()=>({granted:true,blocking:false,checked:true,reason:'no_due_cards',required:0,cards:[]});
  entryResult=undefined;
  const emptyEntry=gate.require('ln',4,0).then(result=>{entryResult=result;});
  await flush();
  assert.equal(entryResult,undefined);
  assert.ok(gate.isOpen());
  assert.match(overlay.querySelector('.pudge-review-gate-card').innerHTML,/No cards are due in this part/);
  overlay.querySelector('[data-review-gate-continue]').click();
  await emptyEntry;
  assert.equal(entryResult,true);
  assert.equal(gate.isOpen(),false);

  // An incomplete provider payload cannot silently authorize entry.
  window.pywebview.api.content_review_begin=async()=>({});
  entryResult=undefined;
  const invalidEntry=gate.require('ln',4,0).then(result=>{entryResult=result;});
  await flush();
  assert.match(overlay.querySelector('.pudge-review-gate-card').innerHTML,/Review status has not been checked/);
  assert.equal(entryResult,undefined);
  gate.close();
  await invalidEntry;
  assert.equal(entryResult,false);

  // A real content card keeps entry blocked until its confirmed review.
  window.pywebview.api.content_review_begin=async()=>({...gateStatus,token:'content-session'});
  window.pywebview.api.content_review_review=async()=>({ok:true,outcome:'confirmed',granted:true,blocking:false,completed:1,remaining:0});
  entryResult=undefined;
  const cardEntry=gate.require('ln',4,1).then(result=>{entryResult=result;});
  await flush();
  assert.equal(entryResult,undefined);
  assert.equal(gate.isOpen(),true);
  gate.showAnswer();
  overlay.querySelector('[data-review-gate-grade="pass"]').click();
  await cardEntry;
  assert.equal(entryResult,true);
  assert.equal(gate.isOpen(),false);
  console.log('review actions S2: PASS');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(()=>clearTimeout(lifecycleTimeout));
let gateStatus = null;
