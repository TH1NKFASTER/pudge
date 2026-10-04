'use strict';
// Reading assistant: safe Markdown, grammar as first answer, follow-up history,
// stale answers ignored after "new chat".
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const nodes = new Map();
function el(tag) {
  const n = {tag, hidden: false, _html: '', dataset: {}, style: {}, children: [], attrs: {}, listeners: {},
    set id(v) { this._id = v; nodes.set(v, this); }, get id() { return this._id; },
    get innerHTML() { return this._html; }, set innerHTML(v) { this._html = String(v); },
    setAttribute(k, v) { this.attrs[k] = v; }, appendChild(c) { this.children.push(c); return c; },
    addEventListener(t, f) { this.listeners[t] = f; },
    querySelector(sel) { return fake(sel); }, querySelectorAll() { return []; }, focus() {} };
  return n;
}
const parts = {};
function fake(sel) { if(!parts[sel]){const dataset={};parts[sel]={get dataset(){return dataset;},textContent: '', title: '', disabled: false, value: '', setAttribute() {}, addEventListener() {}, focus() {}, scrollTop: 0, scrollHeight: 0, set innerHTML(v) { this._html = v; }, get innerHTML() { return this._html || ''; }};}return parts[sel]; }
const calls = [];
let chatResolve;
const window = {pywebview: {api: {
  analyze_grammar: async (t, c, id) => ({status: 'complete', sentence: t, translation: 'I like cats', points: [{id: 'p1', pattern: '～が好き', meaning_in_context: 'like'}], request_id: id}),
  assistant_chat: payload => { calls.push(JSON.parse(JSON.stringify(payload))); return new Promise(r => { chatResolve = r; }); },
}}};
const docListeners = {};
const document = {documentElement: {lang: 'ru'}, body: {appendChild() {}}, createElement: tag => el(tag), getElementById: id => nodes.get(id) || null, addEventListener(type, fn) { (docListeners[type] ||= []).push(fn); }};
const ctx = {window, document, console, Event: function () {}, PudgeReadingTools: {llmAvailable:()=>true,grammar: {html: r => `<ol>${r.points.map(p => `<li>${p.pattern}</li>`).join('')}</ol>`}}};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(require('node:path').join(require('node:path').dirname(process.argv[2]), 'assistant_syntax.js'), 'utf8'), ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const A = ctx.PudgeAssistant;

// 1. Markdown is escaped before formatting.
const md = A.markdownHtml('**が** <img src=x onerror=1>\n\n- 好き\n- だ\n\n`code`');
assert.ok(!md.includes('<img'), 'html escaped');
assert.match(md, /<strong>が<\/strong> &lt;img/);
assert.match(md, /<ul><li>好き<\/li><li>だ<\/li><\/ul>/);
assert.match(md, /<code>code<\/code>/);
// 1b. Fenced code: language label, escaped body, copy button, no Markdown inside.
const code = A.markdownHtml('Пример:\n\n```python\nprint("<b>**x**</b>")\n```\nдальше');
assert.match(code, /<div class="pa-code"><div class="pa-code-head"><span>python<\/span><button type="button" class="pa-code-copy" data-pa-copy-code>/);
assert.match(code, /pa-syntax-function">print<\/span>/);
assert.match(code, /pa-syntax-string">&quot;&lt;b&gt;\*\*x\*\*&lt;\/b&gt;&quot;<\/span>/);
assert.ok(!code.includes('<strong>'), 'no Markdown inside code');
assert.match(A.markdownHtml('```\nls -la\n```'), /<span>code<\/span>.*<code>ls -la<\/code>/s);

(async () => {
  // 2. Grammar opens the panel with user + structured assistant message.
  await A.startGrammar({text: '猫が好きだ。', context: 'ctx'});
  let st = A.state();
  assert.equal(A.isOpen(), true);
  assert.equal(st.thread.messages.length, 2);
  assert.match(st.thread.messages[1].html, /<li>～が好き<\/li>/);
  assert.match(st.thread.messages[1].content, /～が好き — like/, 'grammar summarized for the model');

  // 3. Follow-up sends history (grammar summary as assistant text) + sentence/context/grammar.
  const pendingSend = A.send('Почему が?');
  assert.equal(A.state().pending, true);
  assert.deepEqual(calls[0].messages.map(m => m.role), ['user', 'assistant', 'user']);
  assert.equal(calls[0].sentence, '猫が好きだ。');
  assert.equal(calls[0].context, 'ctx');
  assert.equal(calls[0].grammar.points[0].pattern, '～が好き');
  chatResolve({ok: true, reply: 'Потому что…'});
  await pendingSend;
  st = A.state();
  assert.equal(st.thread.messages.at(-1).content, 'Потому что…');

  // 4. A second send while pending is refused; a stale answer after "new chat" is ignored.
  const s2 = A.send('ещё');
  assert.equal(await A.send('двойной'), false);
  // 4b. Submitting the form (Enter) while pending keeps the draft in the box.
  const input = {value: 'второй вопрос', focus() {}, style: {}, scrollHeight: 20};
  const form = {querySelector: () => input};
  const submit = {target: {closest: sel => (sel === '[data-pa-form]' ? form : null)}, preventDefault() {}};
  const before4b = calls.length;
  docListeners.submit.forEach(fn => fn(submit));
  assert.equal(input.value, 'второй вопрос', 'draft kept while the answer is pending');
  assert.equal(calls.length, before4b, 'nothing sent while pending');
  A.newChat();
  chatResolve({ok: true, reply: 'устарело'});
  await s2;
  assert.equal(A.state().thread.messages.length, 0, 'stale reply did not land in the new chat');

  // 4c. After the answer, the same draft is sent exactly once.
  docListeners.submit.forEach(fn => fn(submit));
  assert.equal(input.value, '', 'draft consumed');
  assert.equal(calls.at(-1).messages.at(-1).content, 'второй вопрос');
  chatResolve({ok: true, reply: 'ok'});
  await new Promise(r => setTimeout(r, 0));
  A.newChat();

  // 5. Collapse / close.
  A.show();
  assert.equal(A.collapseIfOpen(), true);
  assert.equal(A.isOpen(), false);
  assert.equal(A.collapseIfOpen(), false);
  // 6. One continuous chat: a new grammar request appends a topic; the model
  // only gets the current topic; the chat keeps at most 100 messages.
  await A.startGrammar({text: '犬が走る。'});
  const before = A.state().thread.messages.length;
  const s3 = A.send('А тут?');
  const last = calls.at(-1);
  assert.equal(last.sentence, '犬が走る。');
  assert.ok(last.messages.every(m => !m.content.includes('猫')), 'previous topic not sent to the model');
  chatResolve({ok: true, reply: 'ok'});
  await s3;
  assert.ok(A.state().thread.messages.length === before + 2);
  for (let i = 0; i < 70; i += 1) { const p = A.send(`q${i}`); chatResolve({ok: true, reply: `a${i}`}); await p; }
  assert.equal(A.state().thread.messages.length, 100, 'capped at 100 messages');
  // 7. Copy uses the clipboard API.
  const copied = [];
  window.navigator = {clipboard: {writeText: async t => { copied.push(t); }}};
  assert.equal(await A.copyText('答え'), true);
  assert.deepEqual(copied, ['答え']);
  console.log('reading assistant: PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
