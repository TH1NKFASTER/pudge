'use strict';
// G1 grammar UI: code-point spans (non-BMP), overlaps, escaping, invalid spans,
// errors, and stale answers never painting a newer selection.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const elements = new Map();
function el(id) {
  const node = {
    id, _html: '', hidden: false, disabled: false, dataset: {}, style: {removeProperty() {}},
    classList: {_s: new Set(), add(...x) { x.forEach(v => this._s.add(v)); }, remove(...x) { x.forEach(v => this._s.delete(v)); }, contains(v) { return this._s.has(v); }, toggle() {}},
    get innerHTML() { return this._html; }, set innerHTML(v) { this._html = String(v); },
    get textContent() { return this._html; }, set textContent(v) { this._html = String(v); },
    getBoundingClientRect() { return {left: 0, top: 0, bottom: 10, width: 400, height: 200}; },
    querySelector(sel) {
      if (sel === '.pudge-grammar') return grammarRoot;
      if (sel === '[data-pudge-grammar]') return button;
      return null;
    },
    querySelectorAll() { return []; },
    appendChild() {},
  };
  elements.set(id, node);
  return node;
}
const grammarRoot = el('grammar');
const button = el('button');
let created = [];
const document = {
  documentElement: {lang: 'en'},
  body: {appendChild() {}},
  addEventListener() {},
  createElement() { const n = el(`n${created.length}`); created.push(n); return n; },
  getElementById(id) { return id === 'pudgeTranslationPop' ? created[1] || null : null; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
};
let resolveGrammar;
const window = {
  ui:{state:{settings:{llm_enabled:true,llm_model:'test-model',llm_url:'http://127.0.0.1:11434'}}},
  matchMedia() { return {matches: true}; }, innerWidth: 1000, innerHeight: 800,
  addEventListener() {},
  getSelection() { return selection; },
  pywebview: {api: {
    translate_text: async () => ({translation: 'Hello'}),
    analyze_grammar: (text, context, requestId) => new Promise(resolve => { resolveGrammar = value => resolve({...value, request_id: requestId}); }),
  }},
};
const selection = {
  isCollapsed: false, rangeCount: 1,
  getRangeAt() {
    return {
      commonAncestorContainer: {}, getBoundingClientRect() { return {left: 0, top: 0, bottom: 10, width: 10, height: 10}; },
      cloneContents() { return {cloneNode() { return {querySelectorAll() { return []; }, textContent: '😀猫が好きだ。'}; }}; },
      startContainer: {}, startOffset: 0,
    };
  },
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {
  window, document, requestAnimationFrame() {}, getComputedStyle() { return {}; },
  setTimeout() { return 1; }, clearTimeout() {}, console, Date,
}, {filename: 'reading_tools.js'});
const tools = window.PudgeReadingTools;
const html = tools.grammar.html;

// 1. Non-BMP offsets, overlapping points, escaping.
const out = html({
  status: 'complete', sentence: '😀猫が好き<b>',
  points: [
    {id: 'p1', pattern: '～が好き', spans: [{start: 2, end: 5, quote: 'が好き'}], anchored: true, certainty: 'high'},
    {id: 'p2', pattern: '好き', spans: [{start: 3, end: 5, quote: '好き'}], anchored: true},
    {id: 'p3', pattern: '<img src=x onerror=alert(1)>', spans: [{start: 0, end: 1, quote: 'X'}], anchored: true},
  ],
});
assert.match(out, /^<div class="pudge-grammar-sentence" lang="ja">😀猫<span class="pudge-grammar-seg" data-points="p1">が<\/span><span class="pudge-grammar-seg" data-points="p1 p2">好き<\/span>&lt;b&gt;<\/div>/);
assert.ok(!out.includes('<img'), 'model text is escaped');
assert.ok(!out.includes('data-points="p3"'), 'span whose quote does not match is not painted');
assert.match(out, /data-grammar-point="p3"/, 'point itself is still listed');

// 2. Errors and empty result.
assert.match(html({status: 'unavailable', reason: 'llm_disabled'}), /Enable the LLM/);
assert.match(html({status: 'error', reason: 'invalid_structure', detail: '<x>'}), /&lt;x&gt;/);
assert.match(html({status: 'complete', sentence: '猫だ。', points: []}), /No notable grammar points/);

// 3. Stale answer: a new selection/closed popup ignores the old response.
// Configuration gates are independent of API method presence.
const ready={llm_enabled:true,llm_model:'test-model',llm_url:'http://127.0.0.1:11434'};
assert.equal(tools.llmAvailable(ready),true);
assert.equal(tools.llmAvailable({...ready,llm_enabled:false}),false);
assert.equal(tools.llmAvailable({...ready,llm_model:'  '}),false);
assert.equal(tools.llmAvailable({...ready,llm_url:''}),false);
assert.equal(tools.llmAvailable({...ready,llm_provider:'openai',llm_url:'https://api.openai.com/v1',llm_api_key:''}),false);
assert.equal(tools.llmAvailable({...ready,llm_provider:'openai',llm_url:'https://api.openai.com/v1',llm_api_key:'masked'}),true);
assert.equal(tools.llmAvailable({...ready,llm_provider:'openai'}),true);

(async () => {
  const root = {contains() { return true; }, dataset: {}};
  await tools.translation.translateSelection(root, {});
  const pop = created[1];
  assert.match(pop.innerHTML, /data-pudge-grammar/);
  const pending = tools.grammar.analyze();
  tools.translation.hide();          // user closes / starts another selection
  resolveGrammar({status: 'complete', sentence: '😀猫が好きだ。', points: []});
  await pending;
  assert.equal(grammarRoot.innerHTML, 'Analysing…', 'stale answer did not render');

  await tools.translation.translateSelection(root, {});
  const fresh = tools.grammar.analyze();
  resolveGrammar({status: 'complete', sentence: '😀猫が好きだ。', points: []});
  await fresh;
  assert.match(grammarRoot.innerHTML, /No notable grammar points/);

  // 4. Same text, re-created selection object while waiting: the answer is still shown.
  grammarRoot.innerHTML = '';
  const again = tools.grammar.analyze();
  await tools.translation.translateSelection(root, {});
  resolveGrammar({status: 'complete', sentence: '😀猫が好きだ。', points: []});
  await again;
  assert.match(grammarRoot.innerHTML, /No notable grammar points/, 'answer for the same text is not dropped');
  window.ui.state.settings.llm_enabled=false;
  await tools.translation.translateSelection(root, {});
  assert(!pop.innerHTML.includes('data-pudge-grammar'),'disabled LLM leaves no grammar button');
  assert.equal(await tools.grammar.analyze(),false);
  console.log('grammar analysis UI: PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
